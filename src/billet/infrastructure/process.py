"""The subprocess seam: a ``ProcessRunner`` Protocol plus a real implementation.

``ProcessRunner`` is the single point where billet shells out. Tests substitute a fake
to spy on the exact argv passed to ``az`` / ``ssh`` without running anything.
"""

import codecs
from collections.abc import Callable, Sequence
import contextlib
from dataclasses import dataclass
import io
import os
import selectors
import signal
import subprocess
import threading
import time
from typing import IO, Protocol

from billet.shared.errors import ProcessError, ProcessTimeoutError

# A per-line output callback (newline stripped). Streaming merges stdout and stderr —
# docker/BuildKit write build progress to stderr, so a stdout-only tail would be blank.
OnLine = Callable[[str], None]

# Builds the second part of a two-part stdin script from the stdout the first part produced.
Reply = Callable[[str], str]

# Once a conversation's command has exited, how long billet keeps reading its pipes before it
# closes its own ends and returns (D-A8-6). A process the command left behind can hold them
# open for ever (a backgrounded grandchild, or an ssh `ControlPersist` master, which `setsid`s
# out of reach of any group kill); its output is not the command's, and the exit status decides.
_EXIT_GRACE = 2.0

# Exit is not a selectable event, so the conversation loop also wakes this often (seconds) to
# check for it; the grace above counts from the first check that sees it.
_EXIT_POLL_INTERVAL = 0.05

# The most bytes one read takes from a pipe.
_READ_CHUNK = 1 << 16


@dataclass(frozen=True, slots=True)
class CompletedProcess:
    """The outcome of running an external command."""

    argv: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str


class ProcessRunner(Protocol):
    """Runs an external command. The seam tests substitute to spy on argv."""

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        on_line: OnLine | None = None,
        timeout: float | None = None,
    ) -> CompletedProcess:
        """Run ``argv``; raise :class:`ProcessError` on non-zero exit when ``check``.

        ``on_line`` (optional) streams each output line as it arrives; output is still
        captured in full on the returned result either way (the error path needs it).

        ``timeout`` (seconds, optional) bounds a buffered run: on expiry the process is
        killed and :class:`ProcessTimeoutError` is raised regardless of ``check`` (a killed
        process has no meaningful result). It is honored only on the buffered path — a
        streaming (``on_line``) run ignores it, as no streaming caller sets a deadline.
        """
        ...


class ConversationRunner(Protocol):
    """Runs one command whose stdin script is written in two parts, the second computed.

    One process, one session: the caller sends an opening script, reads what it printed up
    to an agreed sentinel line, and only then decides what to send next. ``billet doctor``
    uses it to read ``devcontainer.json`` and then query the service it names over the same
    SSH session (ADR-0015 item 1). The real runner does all of the conversation's I/O in one
    selector loop, with no reader thread, so the deadline caps every wait.
    """

    def converse(
        self,
        argv: Sequence[str],
        *,
        opening: str,
        sentinel: str,
        reply: Reply,
        timeout: float | None = None,
    ) -> CompletedProcess:
        """Run ``argv`` with stdin held open across two writes; never raise on exit status.

        Writes ``opening``, reads stdout until a line equal to ``sentinel``, writes
        ``reply(<stdout up to and including that line>)`` once the opening is fully written,
        closes stdin and collects the rest. If stdout ends before the sentinel (the command
        died, or ssh never connected), or the command stops reading stdin before the opening
        is written, ``reply`` is not called. The result's stdout is everything printed,
        sentinel line included; the caller interprets the exit status.

        ``timeout`` (seconds, optional) bounds the whole conversation, opening through exit,
        every read and write included: on expiry the command's process group is killed, the
        command is reaped and :class:`ProcessTimeoutError` is raised. If ``reply`` (or anything
        else between starting the command and the result, ``KeyboardInterrupt`` included)
        raises, the group is killed and the command reaped too, even if the command itself
        has already exited, and the exception propagates. After a command that exits and a
        conversation that ends normally, whatever still holds its pipes (a process it left
        behind) gets a short grace and is then abandoned, not killed: the call returns the
        command's own result and never waits on a process it did not start.
        """
        ...


class SubprocessRunner:
    """A :class:`ProcessRunner` and :class:`ConversationRunner` backed by :mod:`subprocess`.

    ``exit_grace`` (seconds) is how long :meth:`converse` keeps reading after its command
    exits. It is a parameter only so tests can shorten it: the composition root takes the
    default, and there is no CLI flag.
    """

    def __init__(self, *, exit_grace: float = _EXIT_GRACE) -> None:
        self._exit_grace = exit_grace

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        on_line: OnLine | None = None,
        timeout: float | None = None,
    ) -> CompletedProcess:
        """Run ``argv``, capturing stdout/stderr as text (streamed live when ``on_line``)."""
        if on_line is not None:
            return _run_streaming(list(argv), input_text, check, on_line)
        # argv is always a list built by billet (never shell-interpolated user input).
        try:
            proc = subprocess.run(
                list(argv),
                input=input_text,
                capture_output=True,
                text=True,
                check=False,
                timeout=timeout,
            )
        except subprocess.TimeoutExpired as exc:
            # The child is already killed by subprocess; surface the deadline loudly.
            raise ProcessTimeoutError(tuple(argv), exc.timeout) from exc
        result = CompletedProcess(
            argv=tuple(argv),
            returncode=proc.returncode,
            stdout=proc.stdout,
            stderr=proc.stderr,
        )
        if check and proc.returncode != 0:
            raise ProcessError(result.argv, result.returncode, result.stderr)
        return result

    def converse(
        self,
        argv: Sequence[str],
        *,
        opening: str,
        sentinel: str,
        reply: Reply,
        timeout: float | None = None,
    ) -> CompletedProcess:
        """Run ``argv`` with a two-part stdin script (see :class:`ConversationRunner`).

        One :mod:`selectors` loop on the raw pipe ends does all the I/O (D-A8-6): no thread,
        and no read or write that can block, so every wait is capped by the deadline. Output
        is decoded as UTF-8 with universal newlines, as ``text=True`` decoded it, except that
        an undecodable byte becomes U+FFFD instead of raising: a stray byte in a consumer's
        file is that file's content, not a reason to abandon the conversation.
        """
        # A session of its own: the command and anything it spawns share one process group
        # to kill, and the terminal's Ctrl-C reaches billet alone, never ssh directly.
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        stop_at = None if timeout is None else time.monotonic() + timeout
        exchange: _Exchange | None = None
        exited = False
        try:
            exchange = _Exchange(proc, opening=opening, sentinel=sentinel, reply=reply)
            exited = exchange.run(stop_at, self._exit_grace)
        finally:
            # One cleanup for every exit after Popen: the deadline, a raising reply, an
            # interrupt, a failed setup, or a finish. billet's pipe ends close first, so
            # nothing more is read or written.
            if exchange is not None:
                exchange.close()
            else:
                _close_pipes(proc)
            if not exited:
                _kill_group(proc)
            # Bounded: the command has exited (and is reaped), or was just killed.
            returncode = proc.wait()
        if timeout is not None and not exited:
            raise ProcessTimeoutError(argv, timeout)
        assert exchange is not None  # run() returned, so the exchange was built
        return CompletedProcess(
            argv=tuple(argv),
            returncode=returncode,
            stdout=exchange.stdout.text,
            stderr=exchange.stderr.text,
        )


class _Decoded:
    """One output pipe read so far: decoded as ``text=True`` would, but never raising."""

    def __init__(self) -> None:
        self._decoder = io.IncrementalNewlineDecoder(
            codecs.getincrementaldecoder("utf-8")(errors="replace"), translate=True
        )
        self._parts: list[str] = []
        self.eof = False

    @property
    def text(self) -> str:
        """Everything decoded so far."""
        return "".join(self._parts)

    def feed(self, chunk: bytes) -> str:
        """Decode ``chunk`` (``b""`` marks end of stream) and return the new text."""
        if not chunk:
            self.eof = True
        new: str = self._decoder.decode(chunk, final=self.eof)
        self._parts.append(new)
        return new


class _Exchange:
    """The state of one :meth:`SubprocessRunner.converse` call, driven by one selector loop.

    stdout and stderr are read whenever ready. stdin is non-blocking and registered only
    while bytes are pending, so a holder that keeps it open without reading (a process the
    command left behind, which killing the command does not turn into ``EPIPE``) cannot
    stall a write past the deadline.
    """

    def __init__(
        self, proc: subprocess.Popen[bytes], *, opening: str, sentinel: str, reply: Reply
    ) -> None:
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        self._proc = proc
        self._stdin: IO[bytes] | None = proc.stdin
        self._out_fd = proc.stdout.fileno()
        self._err_fd = proc.stderr.fileno()
        self.stdout = _Decoded()
        self.stderr = _Decoded()
        self._pending = bytearray(opening.encode())
        self._sentinel = sentinel
        self._reply = reply
        self._line = ""  # the stdout line not yet ended, while looking for the sentinel
        self._scanned = 0  # characters of stdout already split into lines
        self._sentinel_end: int | None = None  # stdout length through the sentinel line
        self._replied = False  # nothing more will be queued for stdin
        self._selector = selectors.DefaultSelector()
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            os.set_blocking(stream.fileno(), False)
        self._selector.register(self._out_fd, selectors.EVENT_READ)
        self._selector.register(self._err_fd, selectors.EVENT_READ)

    def run(self, stop_at: float | None, grace: float) -> bool:
        """Converse until the command has exited and its output is drained or ``grace`` is up.

        Returns ``True`` then, or ``False`` if the monotonic ``stop_at`` passes while the
        command is still running.
        """
        drain_by: float | None = None
        while True:
            self._advance()
            if drain_by is None and self._proc.poll() is not None:
                drain_by = time.monotonic() + grace
            now = time.monotonic()
            if drain_by is not None and (now >= drain_by or (self.stdout.eof and self.stderr.eof)):
                return True
            if stop_at is not None and now >= stop_at:
                return drain_by is not None  # a command that has exited is not timed out
            wait = min([_EXIT_POLL_INTERVAL, *(t - now for t in (stop_at, drain_by) if t)])
            for key, _events in self._selector.select(wait):
                if key.fd == self._out_fd:
                    self._read_stdout()
                elif key.fd == self._err_fd:
                    self._read(self._err_fd, self.stderr)
                else:
                    self._write()

    def close(self) -> None:
        """Close the selector and billet's pipe ends; anything unread or unsent is dropped."""
        self._selector.close()
        _close_pipes(self._proc)
        self._stdin = None

    def _advance(self) -> None:
        """Queue the reply when it is due; close stdin once nothing more will be written."""
        if self._stdin is None:
            return
        if not self._pending and self._sentinel_end is not None and not self._replied:
            # The opening is fully written and the sentinel has been read: reply now.
            self._replied = True
            self._pending += self._reply(self.stdout.text[: self._sentinel_end]).encode()
        finished = self._replied or (self.stdout.eof and self._sentinel_end is None)
        if not self._pending and finished:
            self._close_stdin()  # the end of the script: the command reads end of file
            return
        fd = self._stdin.fileno()
        registered = fd in self._selector.get_map()
        if self._pending and not registered:
            self._selector.register(fd, selectors.EVENT_WRITE)
        elif not self._pending and registered:
            self._selector.unregister(fd)

    def _write(self) -> None:
        assert self._stdin is not None  # only an open stdin is ever registered
        try:
            written = os.write(self._stdin.fileno(), self._pending)
        except BlockingIOError:
            return
        except BrokenPipeError:
            # The command stopped reading; its exit status decides. The rest of the script
            # is dropped, and if the opening did not get through, no reply is built.
            self._pending.clear()
            self._replied = True
            self._close_stdin()
            return
        del self._pending[:written]

    def _close_stdin(self) -> None:
        assert self._stdin is not None
        with contextlib.suppress(KeyError):
            self._selector.unregister(self._stdin.fileno())
        self._stdin.close()  # its buffer is empty (writes go to the fd), so this cannot fail
        self._stdin = None

    def _read(self, fd: int, sink: _Decoded) -> str:
        try:
            chunk = os.read(fd, _READ_CHUNK)
        except BlockingIOError:
            return ""
        if not chunk:
            self._selector.unregister(fd)
        return sink.feed(chunk)

    def _read_stdout(self) -> None:
        """Read stdout, and look for the sentinel line in it until it has been seen."""
        new = self._read(self._out_fd, self.stdout)
        if self._sentinel_end is not None:
            return
        self._line += new
        while "\n" in self._line:
            line, self._line = self._line.split("\n", 1)
            self._scanned += len(line) + 1
            if line == self._sentinel:
                self._sentinel_end = self._scanned
                return
        if self.stdout.eof and self._line == self._sentinel:  # an unterminated last line
            self._sentinel_end = self._scanned + len(self._line)


def _kill_group(proc: subprocess.Popen[bytes]) -> None:
    """SIGKILL the command's whole process group, whether or not the command was reaped.

    The group's id is the command's pid (``start_new_session``). A command that exited and
    was reaped mid-conversation can leave members behind, and the group lives on while it
    has any: POSIX does not reuse a pid as a group id while that group has members, so the
    signal reaches only processes the command started. A group with no members left is
    gone (``ProcessLookupError``); macOS answers ``PermissionError`` for a group of zombies
    alone, which is gone just the same.
    """
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(proc.pid, signal.SIGKILL)


def _close_pipes(proc: subprocess.Popen[bytes]) -> None:
    """Close billet's ends of the command's pipes; anything unread or unsent is dropped."""
    for stream in (proc.stdin, proc.stdout, proc.stderr):
        if stream is not None:
            # Writes go to the raw fd, so no buffer is left to flush; suppressed all the
            # same, as cleanup must never mask the exception that brought it here.
            with contextlib.suppress(OSError):
                stream.close()


def _pump(stream: IO[str], sink: list[str], on_line: OnLine) -> None:
    """Drain one pipe line by line: capture verbatim, emit with the newline stripped."""
    for line in stream:
        sink.append(line)
        on_line(line.rstrip("\n"))


def _run_streaming(
    argv: list[str], input_text: str | None, check: bool, on_line: OnLine
) -> CompletedProcess:
    """Run via ``Popen``, one reader thread per pipe so neither can deadlock the other."""
    proc = subprocess.Popen(
        argv,
        stdin=subprocess.PIPE if input_text is not None else None,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    assert proc.stdout is not None and proc.stderr is not None  # both are PIPE above
    stdout_lines: list[str] = []
    stderr_lines: list[str] = []
    readers = (
        threading.Thread(target=_pump, args=(proc.stdout, stdout_lines, on_line), daemon=True),
        threading.Thread(target=_pump, args=(proc.stderr, stderr_lines, on_line), daemon=True),
    )
    for reader in readers:
        reader.start()
    if input_text is not None and proc.stdin is not None:
        try:
            proc.stdin.write(input_text)
        except BrokenPipeError:
            pass  # the command exited before reading all of stdin; its exit code decides
        finally:
            proc.stdin.close()
    for reader in readers:
        reader.join()
    returncode: int = proc.wait()
    result = CompletedProcess(
        argv=tuple(argv),
        returncode=returncode,
        stdout="".join(stdout_lines),
        stderr="".join(stderr_lines),
    )
    if check and returncode != 0:
        raise ProcessError(result.argv, result.returncode, result.stderr)
    return result
