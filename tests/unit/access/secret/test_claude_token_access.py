"""Tests for ClaudeTokenAccess — local, loud resolution of CLAUDE_CODE_OAUTH_TOKEN."""

from collections.abc import Callable

import pytest

from billet.access.secret.claude_token_access import ClaudeTokenAccess
from billet.infrastructure.process import CompletedProcess
from billet.shared.errors import ConfigError, ProcessError
from tests.unit._fakes import FakeProcessRunner, completed

Handler = Callable[[list[str]], CompletedProcess]


def _access(handler: Handler) -> tuple[ClaudeTokenAccess, FakeProcessRunner]:
    runner = FakeProcessRunner(handler)
    return ClaudeTokenAccess(runner), runner


def test_empty_command_is_disabled_and_runs_nothing() -> None:
    access, runner = _access(lambda _argv: completed())
    assert access.resolve("") is None
    assert access.resolve("   ") is None
    assert runner.calls == []  # the feature is off: no subprocess at all


def test_resolve_returns_token_and_trims_trailing_newline() -> None:
    access, runner = _access(lambda _argv: completed(stdout="tok-abc123\n"))
    assert access.resolve("op read op://x/token") == "tok-abc123"
    # Runs the operator's command as-is through a shell, bounded by a timeout.
    assert runner.calls[-1] == ("sh", "-c", "op read op://x/token")
    assert runner.timeouts[-1] is not None


def test_resolve_strips_surrounding_whitespace() -> None:
    access, _ = _access(lambda _argv: completed(stdout="  tok-xyz  \n"))
    assert access.resolve("printenv CLAUDE_CODE_OAUTH_TOKEN") == "tok-xyz"


def test_non_zero_exit_raises_and_surfaces_stderr() -> None:
    access, _ = _access(
        lambda _argv: completed(returncode=1, stderr="[ERROR] not signed in to 1Password")
    )
    with pytest.raises(ConfigError, match="not signed in to 1Password") as exc:
        access.resolve("op read op://x/token")
    assert "claude_token_cmd failed" in str(exc.value)


def test_empty_stdout_raises() -> None:
    access, _ = _access(lambda _argv: completed(stdout="   \n"))
    with pytest.raises(ConfigError, match="no token on STDOUT"):
        access.resolve("printenv CLAUDE_CODE_OAUTH_TOKEN")


def test_timeout_raises_a_clear_error() -> None:
    def _timeout(_argv: list[str]) -> CompletedProcess:
        # The real runner raises ProcessError when it kills a run past its deadline.
        raise ProcessError(("sh", "-c", "hangs"), -1, "timed out after 30s")

    access, _ = _access(_timeout)
    with pytest.raises(ConfigError, match="timed out"):
        access.resolve("hangs")


def test_token_value_never_appears_in_any_argv() -> None:
    access, runner = _access(lambda _argv: completed(stdout="super-secret-token\n"))
    access.resolve("fetch-it")
    for command in runner.commands():
        assert "super-secret-token" not in command
