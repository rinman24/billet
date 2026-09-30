"""Tests for the real SubprocessRunner seam (the one place billet shells out)."""

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


def test_run_timeout_raises_the_typed_timeout_error() -> None:
    with pytest.raises(ProcessTimeoutError) as exc_info:
        SubprocessRunner().run([sys.executable, "-c", "import time; time.sleep(60)"], timeout=0.2)
    assert exc_info.value.timeout == 0.2
    assert exc_info.value.returncode == -1


# --- converse: broken pipes, the deadline and cleanup (D-A7-2, D-A7-4, D-A7-7) ------------
#
# Real children, never ssh. Each test records the Popen it spawned, so "no child survives"
# is asserted on the process itself: a returncode is set only once the child is reaped.

# Larger than any pipe buffer, so the write blocks until the child has gone and then fails
# with EPIPE: the broken-pipe paths are deterministic rather than a race.
_FLOOD = "x" * (1 << 20)

# Prints the sentinel, then outlives any test deadline without reading stdin (one process:
# `exec` leaves no grandchild holding the pipes).
_END_THEN_HANG = ["sh", "-c", "echo END; exec sleep 60"]


@pytest.fixture
def spawned(monkeypatch: pytest.MonkeyPatch) -> list[subprocess.Popen[str]]:
    """Record every Popen the runner creates (the real class, merely remembered)."""
    children: list[subprocess.Popen[str]] = []

    class _Recording(subprocess.Popen[str]):
        def __init__(self, *args: Any, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            children.append(self)

    monkeypatch.setattr(subprocess, "Popen", _Recording)
    return children


def test_converse_survives_a_command_that_exits_before_reading_the_opening(
    spawned: list[subprocess.Popen[str]],
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
    spawned: list[subprocess.Popen[str]],
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


@pytest.mark.parametrize(
    "argv",
    [
        pytest.param([sys.executable, "-c", "import time; time.sleep(60)"], id="in-the-opening"),
        pytest.param(_END_THEN_HANG, id="after-the-reply"),
    ],
)
def test_converse_kills_and_reaps_a_command_that_outlives_the_deadline(
    spawned: list[subprocess.Popen[str]], argv: list[str]
) -> None:
    started = time.monotonic()
    with pytest.raises(ProcessTimeoutError) as exc_info:
        SubprocessRunner().converse(
            argv, opening="", sentinel="END", reply=lambda _out: "", timeout=0.5
        )
    assert time.monotonic() - started < 10  # bounded by the deadline, not the child's sleep
    assert exc_info.value.timeout == 0.5
    assert exc_info.value.argv == tuple(argv)
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]  # killed and reaped


def test_converse_within_the_deadline_returns_normally(
    spawned: list[subprocess.Popen[str]],
) -> None:
    result = SubprocessRunner().converse(
        ["bash", "-se"], opening="echo END\n", sentinel="END", reply=lambda _o: "", timeout=30
    )
    assert result.returncode == 0
    assert [child.returncode for child in spawned] == [0]


def test_a_raising_reply_propagates_and_leaves_no_child_running(
    spawned: list[subprocess.Popen[str]],
) -> None:
    def reply(_out: str) -> str:
        raise RuntimeError("reply failed")

    started = time.monotonic()
    with pytest.raises(RuntimeError, match="reply failed"):
        SubprocessRunner().converse(_END_THEN_HANG, opening="", sentinel="END", reply=reply)
    assert time.monotonic() - started < 10
    assert [child.returncode for child in spawned] == [-signal.SIGKILL]
