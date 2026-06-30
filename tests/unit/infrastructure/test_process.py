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
