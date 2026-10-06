"""Tests for the real SubprocessRunner seam (the one place billet shells out)."""

from collections.abc import Iterator
import contextlib
import os
from pathlib import Path
import signal
import subprocess
import sys
import time
from typing import Any

import pytest

from billet.infrastructure.process import SubprocessRunner
from billet.shared.errors import ProcessError, ProcessTimeoutError


def test_runner_captures_stdout_and_argv() -> None:
    result = SubprocessRunner().run(["echo", "hello"])
    assert result.returncode == 0
    assert result.stdout.strip() == "hello"
    assert result.argv == ("echo", "hello")


def test_runner_passes_stdin_through() -> None:
    result = SubprocessRunner().run(["cat"], input_text="piped")
    assert result.stdout == "piped"


def test_runner_raises_process_error_on_nonzero_when_checked() -> None:
    with pytest.raises(ProcessError) as exc_info:
        SubprocessRunner().run(["false"])
    assert exc_info.value.returncode != 0
    assert exc_info.value.argv == ("false",)


def test_runner_returns_nonzero_without_check() -> None:
    result = SubprocessRunner().run(["false"], check=False)
    assert result.returncode != 0


def test_runner_streams_lines_from_both_pipes_and_still_captures() -> None:
    lines: list[str] = []
    result = SubprocessRunner().run(
        ["sh", "-c", "echo out1; echo err1 1>&2; echo out2"], on_line=lines.append
    )
    assert result.returncode == 0
    assert result.stdout == "out1\nout2\n"  # capture is verbatim
    assert result.stderr == "err1\n"  # stderr captured too (build progress lives there)
    assert set(lines) == {"out1", "out2", "err1"}  # streamed, newline-stripped


def test_runner_streaming_passes_stdin_through() -> None:
    lines: list[str] = []
    result = SubprocessRunner().run(["cat"], input_text="piped\n", on_line=lines.append)
    assert result.stdout == "piped\n"
    assert lines == ["piped"]


def test_runner_streaming_raises_process_error_with_captured_stderr() -> None:
    lines: list[str] = []
    with pytest.raises(ProcessError) as exc_info:
        SubprocessRunner().run(["sh", "-c", "echo boom 1>&2; exit 3"], on_line=lines.append)
    assert exc_info.value.returncode == 3
    assert "boom" in exc_info.value.stderr
    assert "boom" in lines


# --- converse: a two-part stdin script over one process -------------------------------


def test_converse_sends_the_reply_computed_from_the_output_up_to_the_sentinel() -> None:
    seen: list[str] = []

    def reply(out: str) -> str:
        seen.append(out)
        return f"echo got {len(out.splitlines())}\n"

    result = SubprocessRunner().converse(
        ["bash", "-se"], opening="echo one\necho END\n", sentinel="END", reply=reply
    )
    assert seen == ["one\nEND\n"]
    assert result.stdout == "one\nEND\ngot 2\n"
    assert result.returncode == 0
    assert result.argv == ("bash", "-se")


def test_converse_skips_the_reply_when_the_command_ends_before_the_sentinel() -> None:
    called: list[str] = []
    result = SubprocessRunner().converse(
        ["bash", "-c", "echo partial; echo oops >&2; exit 3"],
        opening="",
        sentinel="END",
        reply=lambda out: called.append(out) or "",
    )
    assert called == []
    assert result.returncode == 3  # never raises: the caller reads the status
    assert result.stdout == "partial\n"
    assert result.stderr == "oops\n"


def test_converse_hands_the_reply_only_the_output_through_the_sentinel_line() -> None:
    seen: list[str] = []
    result = SubprocessRunner().converse(
        ["sh", "-c", "printf 'a\\nEND\\nafter\\n'; cat >/dev/null"],
        opening="",
        sentinel="END",
        reply=lambda out: seen.append(out) or "",
    )
    assert seen == ["a\nEND\n"]  # even if `after` arrived in the same read
    assert result.stdout == "a\nEND\nafter\n"


def test_converse_decodes_like_text_mode_and_keeps_an_unterminated_last_line() -> None:
    seen: list[str] = []
    result = SubprocessRunner().converse(
        ["sh", "-c", "printf 'a\\r\\nb\\377\\r\\n' >&2; printf 'x\\r\\nEND'"],
        opening="",
        sentinel="END",
        reply=lambda out: seen.append(out) or "",
    )
    assert seen == ["x\nEND"]  # the sentinel as the last, unterminated line still counts
    assert result.stdout == "x\nEND"
    assert result.stderr == "a\nb�\n"  # universal newlines; a bad byte is replaced


def test_converse_drains_both_pipes_past_their_buffers() -> None:
    script = "import sys; sys.stderr.write('e' * (1 << 20)); print('o' * (1 << 20)); print('END')"
    result = SubprocessRunner().converse(
        [sys.executable, "-c", script], opening="", sentinel="END", reply=lambda _o: ""
    )
    assert result.returncode == 0
    assert len(result.stderr) == 1 << 20
    assert result.stdout.endswith("END\n")


def test_run_timeout_raises_the_typed_timeout_error() -> None:
    with pytest.raises(ProcessTimeoutError) as exc_info:
        SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.2)
    assert exc_info.value.timeout == 0.2
    assert exc_info.value.returncode == -1


# --- converse: broken pipes, the deadline and cleanup (D-A7-2, D-A7-4, D-A8-6) ------------
#
# Real children, never ssh. Each test records the Popen it spawned, so "no child survives"
# is asserted on the process itself: a returncode is set only once the child is reaped.

# Larger than any pipe buffer, so the write cannot complete until the child has gone and then
# fails with EPIPE: the broken-pipe paths are deterministic rather than a race.
_FLOOD = "x" * (1 << 20)

# Prints the sentinel, then outlives any test deadline without reading stdin (one process:
# `exec` leaves no grandchild holding the pipes).
_END_THEN_HANG = ["sh", "-c", "echo END; exec sleep 60"]

# Short enough for a fast suite, with the deadline and the grace each well under the 5 s the
# bounded tests allow.
_DEADLINE = 1.0
_GRACE = 1.0


class _Spawned(list[subprocess.Popen[bytes]]):
    """The Popen objects the runner created, and the keyword arguments each was given."""

    def __init__(self) -> None:
        super().__init__()
        self.kwargs: list[dict[str, Any]] = []


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> _Spawned:
    """Record every Popen the runner creates (the real class, merely remembered)."""
    children = _Spawned()

    class _Recording(subprocess.Popen[bytes]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            children.append(self)
            children.kwargs.append(kwargs)

    monkeypatch.setattr(subprocess, "Popen", _Recording)
    return children


@pytest.fixture
def killpg_calls(monkeypatch: pytest.MonkeyPatch) -> list[tuple[int, int]]:
    """Spy on ``os.killpg``: record each call, then really send the signal."""
    calls: list[tuple[int, int]] = []
    real = os.killpg

    def spy(pgid: int, sig: int) -> None:
        calls.append((pgid, sig))
        real(pgid, sig)

    monkeypatch.setattr(os, "killpg", spy)
    return calls


def _gone(pid: int) -> bool:
    """Whether ``pid`` has ended (absent, or a zombie its new parent has yet to reap)."""
    ps = subprocess.run(
        ["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False
    )
    state = ps.stdout.strip()
    return not state or state.startswith("Z")


def _wait_gone(pid: int, within: float = 5.0) -> bool:
    stop = time.monotonic() + within
    while not _gone(pid):
        if time.monotonic() >= stop:
            return False
        time.sleep(0.05)
    return True


@pytest.fixture
def orphans(tmp_path: Path) -> Iterator[Path]:
    """A file the child appends the pid of each process it leaves behind to.

    A process the command left behind is outside billet's reach once the command has exited,
    so the test reaps it: teardown kills every recorded pid still running and asserts that
    none is left.
    """
    pidfile = tmp_path / "orphans.pid"
    yield pidfile
    pids = [int(pid) for pid in pidfile.read_text().split()] if pidfile.exists() else []
    for pid in pids:
        if not _gone(pid):
            with contextlib.suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    assert pids, "the child recorded no orphan: the test did not exercise one"
    assert all(_wait_gone(pid) for pid in pids)


def _orphan_pids(pidfile: Path) -> list[int]:
    return [int(pid) for pid in pidfile.read_text().split()]


def test_converse_survives_a_command_that_exits_before_reading_the_opening(
    spawned: _Spawned,
) -> None:
    called: list[str] = []
    result = SubprocessRunner().converse(
        ["sh", "-c", "echo END; exit 4"],
        opening=_FLOOD,  # the opening write hits the broken pipe
        sentinel="END",
        reply=lambda out: called.append(out) or "",
    )
    assert result.returncode == 4  # the command's own status, no exception
    assert result.stdout == "END\n"
    assert called == []  # nobody is reading, so no reply is built
    assert [child.returncode for child in spawned] == [4]


def test_converse_survives_a_command_that_exits_after_the_sentinel(
    spawned: _Spawned,
) -> None:
    called: list[str] = []

    def reply(out: str) -> str:
        called.append(out)
        return _FLOOD  # the reply write hits the broken pipe

    result = SubprocessRunner().converse(
        ["sh", "-c", "echo END; exit 5"], opening="", sentinel="END", reply=reply
    )
    assert called == ["END\n"]
    assert result.returncode == 5
    assert result.stdout == "END\n"
    assert [child.returncode for child in spawned] == [5]


def test_converse_treats_a_broken_pipe_on_the_last_write_as_the_command_done_reading(
    spawned: _Spawned,
) -> None:
    # What the old `stdin.close()` BrokenPipeError handler caught: a final write, small enough
    # to fit any buffer, to a command that has stopped reading. The command closes its only
    # read end of stdin before printing the sentinel, so the reply's write fails with EPIPE
    # every time; it is reported as the command's own exit, never raised.
    result = SubprocessRunner().converse(
        ["sh", "-c", "exec 0<&-; echo END; sleep 0.2; exit 6"],
        opening="",
        sentinel="END",
        reply=lambda _out: "echo never run\n",
    )
    assert result.returncode == 6
    assert result.stdout == "END\n"
    assert [child.returncode for child in spawned] == [6]


@pytest.mark.parametrize(
    ("argv", "opening"),
    [
        pytest.param(
            [sys.executable, "-c", "import time; time.sleep(60)"], "", id="never-the-sentinel"
        ),
        # The opening outgrows the pipe buffer and the child never reads it: the write itself
        # must give way to the deadline.
        pytest.param(["sh", "-c", "exec sleep 60"], _FLOOD, id="in-the-opening"),
        pytest.param(_END_THEN_HANG, "", id="after-the-reply"),
    ],
)
def test_converse_kills_and_reaps_a_command_that_outlives_the_deadline(
    spawned: _Spawned, killpg_calls: list[tuple[int, int]], argv: list[str], opening: str
) -> None:
    started = time.monotonic()
    with pytest.raises(ProcessTimeoutError) as exc_info:
        SubprocessRunner().converse(
            argv, opening=opening, sentinel="END", reply=lambda _out: "", timeout=0.5
        )
    assert time.monotonic() - started < 5  # bounded by the deadline, not the child's sleep
    assert exc_info.value.timeout == 0.5
    assert exc_info.value.argv == tuple(argv)
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]  # killed and reaped
    assert [kwargs["start_new_session"] for kwargs in spawned.kwargs] == [True]
    assert killpg_calls == [(spawned[0].pid, signal.SIGKILL)]  # the whole group


def test_converse_within_the_deadline_returns_normally(
    spawned: _Spawned, killpg_calls: list[tuple[int, int]]
) -> None:
    result = SubprocessRunner().converse(
        ["bash", "-se"], opening="echo END\n", sentinel="END", reply=lambda _o: "", timeout=30
    )
    assert result.returncode == 0
    assert [child.returncode for child in spawned] == [0]
    assert killpg_calls == []  # a command that exits is never signalled


@pytest.mark.parametrize("error", [RuntimeError, KeyboardInterrupt])
def test_a_raising_reply_propagates_and_leaves_no_child_running(
    spawned: _Spawned, error: type[BaseException]
) -> None:
    def reply(_out: str) -> str:
        raise error("reply failed")

    started = time.monotonic()
    with pytest.raises(error, match="reply failed"):
        SubprocessRunner().converse(_END_THEN_HANG, opening="", sentinel="END", reply=reply)
    assert time.monotonic() - started < 5
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]


def test_a_raising_reply_kills_the_whole_process_group(spawned: _Spawned, orphans: Path) -> None:
    def reply(_out: str) -> str:
        raise RuntimeError("reply failed")

    argv = ["sh", "-c", '(sleep 60 & echo $! >> "$1"); echo END; exec sleep 60', "sh", str(orphans)]
    with pytest.raises(RuntimeError, match="reply failed"):
        SubprocessRunner().converse(argv, opening="", sentinel="END", reply=reply)
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]
    assert all(_wait_gone(pid) for pid in _orphan_pids(orphans))  # the grandchild too


# --- converse: processes the command leaves behind (D-A8-6) -----------------------------
#
# A grandchild that inherits the pipes keeps them open after the command exits. The command's
# exit, not the pipes' end of file, decides how long billet waits.


def test_an_orphan_holding_the_pipes_does_not_hold_a_command_that_exited(
    spawned: _Spawned, orphans: Path
) -> None:
    argv = ["sh", "-c", '(sleep 30 & echo $! >> "$1"); echo END; exit 0', "sh", str(orphans)]
    started = time.monotonic()
    result = SubprocessRunner(exit_grace=_GRACE).converse(
        argv, opening="", sentinel="END", reply=lambda _o: "", timeout=_DEADLINE
    )
    assert time.monotonic() - started < 5
    assert result.returncode == 0  # a normal result: the exit status decides
    assert result.stdout == "END\n"
    assert [child.returncode for child in spawned] == [0]
    assert not any(_gone(pid) for pid in _orphan_pids(orphans))  # it held the pipes throughout


def test_a_command_that_outlives_the_deadline_with_an_orphan_still_times_out(
    spawned: _Spawned, orphans: Path
) -> None:
    argv = ["sh", "-c", '(sleep 30 & echo $! >> "$1"); exec sleep 60', "sh", str(orphans)]
    started = time.monotonic()
    with pytest.raises(ProcessTimeoutError):
        SubprocessRunner(exit_grace=_GRACE).converse(
            argv, opening="", sentinel="END", reply=lambda _o: "", timeout=_DEADLINE
        )
    assert time.monotonic() - started < 5
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]
    assert all(_wait_gone(pid) for pid in _orphan_pids(orphans))  # same group: killed too


def test_a_daemonised_holder_of_stdin_cannot_stall_the_opening_past_the_deadline(
    spawned: _Spawned, orphans: Path
) -> None:
    # A holder in a session of its own (as a `ControlPersist` master is) survives the group
    # kill and keeps stdin open without reading, so the blocked write never sees EPIPE: only
    # a non-blocking write abandoned at the deadline ends the call. fd 3 carries stdin into
    # the background job, which a non-interactive shell would otherwise give /dev/null.
    holder = "import os, time; os.setsid(); time.sleep(30)"
    script = 'exec 3<&0; "$2" -c "$3" <&3 3<&- & echo $! >> "$1"; exec sleep 60'
    argv = ["sh", "-c", script, "sh", str(orphans), sys.executable, holder]
    started = time.monotonic()
    with pytest.raises(ProcessTimeoutError):
        SubprocessRunner(exit_grace=_GRACE).converse(
            argv, opening=_FLOOD, sentinel="END", reply=lambda _o: "", timeout=_DEADLINE
        )
    assert time.monotonic() - started < 5
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]
    assert not any(_gone(pid) for pid in _orphan_pids(orphans))  # out of the group's reach
