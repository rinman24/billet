"""The subprocess seam: a ``ProcessRunner`` Protocol plus a real implementation.

``ProcessRunner`` is the single point where billet shells out. Tests substitute a fake
to spy on the exact argv passed to ``az`` / ``ssh`` without running anything.
"""

from collections.abc import Callable, Sequence
from dataclasses import dataclass
import subprocess
import threading
from typing import IO, Protocol

from billet.shared.errors import ProcessError

# A per-line output callback (newline stripped). Streaming merges stdout and stderr —
# docker/BuildKit write build progress to stderr, so a stdout-only tail would be blank.
OnLine = Callable[[str], None]

# Builds the second part of a two-part stdin script from the stdout the first part produced.
Reply = Callable[[str], str]

# Sentinel returncode reported when a run is killed for exceeding its timeout — a killed
# process has no meaningful exit status, so callers key on the raised ProcessError instead.
_TIMEOUT_RC = -1


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
        killed and :class:`ProcessError` is raised regardless of ``check`` (a killed
        process has no meaningful result). It is honored only on the buffered path — a
        streaming (``on_line``) run ignores it, as no streaming caller sets a deadline.
        """
        ...


class ConversationRunner(Protocol):
    """Runs one command whose stdin script is written in two parts, the second computed.

    One process, one session: the caller sends an opening script, reads what it printed up
    to an agreed sentinel line, and only then decides what to send next. ``billet doctor``
    uses it to read ``devcontainer.json`` and then query the service it names over the same
    SSH session (ADR-0015 item 1).
    """

    def converse(
        self, argv: Sequence[str], *, opening: str, sentinel: str, reply: Reply
    ) -> CompletedProcess:
        """Run ``argv`` with stdin held open across two writes; never raise on exit status.

        Writes ``opening``, reads stdout until a line equal to ``sentinel``, writes
        ``reply(<stdout so far>)``, closes stdin and collects the rest. If stdout ends before
        the sentinel (the command died, or ssh never connected), ``reply`` is not called.
        The result's stdout is everything printed, sentinel line included; the caller
        interprets the exit status.
        """
        ...


class SubprocessRunner:
    """A :class:`ProcessRunner` and :class:`ConversationRunner` backed by :mod:`subprocess`."""

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
            raise ProcessError(
                tuple(argv), _TIMEOUT_RC, f"timed out after {exc.timeout:g}s"
            ) from exc
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
        self, argv: Sequence[str], *, opening: str, sentinel: str, reply: Reply
    ) -> CompletedProcess:
        """Run ``argv`` with a two-part stdin script (see :class:`ConversationRunner`)."""
        proc = subprocess.Popen(
            list(argv),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        assert proc.stdin is not None and proc.stdout is not None and proc.stderr is not None
        stderr_lines: list[str] = []
        drain = threading.Thread(target=_collect, args=(proc.stderr, stderr_lines), daemon=True)
        drain.start()
        stdout_lines: list[str] = []
        open_ = _send(proc.stdin, opening)
        reached = False
        # readline, not iteration: the loop stops mid-stream and resumes after the reply.
        while line := proc.stdout.readline():
            stdout_lines.append(line)
            if line.rstrip("\n") == sentinel:
                reached = True
                break
        if reached and open_:
            _send(proc.stdin, reply("".join(stdout_lines)))
        try:
            proc.stdin.close()
        except BrokenPipeError:
            pass  # the command already exited; its exit status decides
        stdout_lines.extend(proc.stdout.readlines())
        drain.join()
        return CompletedProcess(
            argv=tuple(argv),
            returncode=proc.wait(),
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
        )


def _send(stdin: IO[str], text: str) -> bool:
    """Write and flush ``text``; return ``False`` if the command has stopped reading."""
    try:
        stdin.write(text)
        stdin.flush()
    except BrokenPipeError:
        return False
    return True


def _collect(stream: IO[str], sink: list[str]) -> None:
    """Drain one pipe into ``sink`` so the other cannot deadlock behind it."""
    sink.extend(stream)


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
