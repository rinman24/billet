"""Tests for GitSourceAccess — agent-forwarded clone, else fetch + safe fast-forward over SSH."""

from billet.access.source.git_source_access import GitSourceAccess
from billet.contracts import WorkspaceSpec
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

SPEC = make_workspace_spec()
REMOTE = make_remote_host()


def _emitted_script(spec: WorkspaceSpec) -> str:
    """Return the remote bash GitSourceAccess emits (the final ssh argv element)."""
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(spec, REMOTE)
    # The tty path passes the whole script verbatim as the last ssh argument.
    return runner.calls[-1][-1]


def test_ensure_clone_uses_agent_forwarding_and_a_tty() -> None:
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    cmd = runner.commands()[-1]
    assert cmd.startswith("ssh ")
    assert " -A " in f" {cmd} "  # agent forwarding — key stays on the operator's machine
    assert " -t " in f" {cmd} "  # tty
    assert "azureuser@20.0.0.5" in cmd


def test_ensure_clone_script_is_idempotent_clone_or_fetch() -> None:
    script = _emitted_script(SPEC)
    assert "git clone" in script
    assert "fetch --prune" in script
    assert "git@github.com:genshift/gswa-backend.git" in script


def test_script_advances_only_via_non_destructive_fast_forward() -> None:
    # The advance is a guarded ff-only merge against the branch upstream.
    assert "merge --ff-only '@{u}'" in _emitted_script(SPEC)


def test_script_carries_every_skip_guard() -> None:
    script = _emitted_script(SPEC)
    # Detached HEAD is detected via a quiet symbolic-ref (empty branch name).
    assert "symbolic-ref --quiet --short HEAD" in script
    assert "HEAD is detached" in script
    # No upstream is detected via rev-parse @{u} in a conditional.
    assert "rev-parse --abbrev-ref --symbolic-full-name '@{u}'" in script
    assert "has no upstream" in script
    # Dirty check ignores untracked files so a bootstrap-written .env never blocks the advance.
    assert "status --porcelain --untracked-files=no" in script
    assert "tracked files dirty" in script
    # Non-ff / diverged / untracked-would-be-overwritten warns and continues.
    assert "cannot fast-forward" in script


def test_script_never_contains_destructive_commands() -> None:
    script = _emitted_script(SPEC)
    assert "reset --hard" not in script
    assert "checkout --" not in script
    assert "git clean" not in script
    assert "git pull" not in script


def test_script_warnings_are_all_billet_source_prefixed() -> None:
    script = _emitted_script(SPEC)
    # Every skip branch is a one-line, prefixed, non-fatal warning (script exits 0 on skip).
    for reason in (
        "HEAD is detached",
        "has no upstream",
        "tracked files dirty",
        "cannot fast-forward",
    ):
        line = next(ln for ln in script.splitlines() if reason in ln)
        assert "[billet/source]" in line
