"""Tests for the real SubprocessRunner seam (the one place billet shells out)."""

import pytest

from billet.infrastructure.process import SubprocessRunner
from billet.shared.errors import ProcessError


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
