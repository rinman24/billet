"""Tests for GitSourceAccess — agent-forwarded, idempotent clone/fetch over SSH."""

from billet.access.source.git_source_access import GitSourceAccess
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

SPEC = make_workspace_spec()
REMOTE = make_remote_host()


def test_ensure_clone_uses_agent_forwarding_and_a_tty() -> None:
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    cmd = runner.commands()[-1]
    assert cmd.startswith("ssh ")
    assert " -A " in f" {cmd} "  # agent forwarding — key stays on the operator's machine
    assert " -t " in f" {cmd} "  # tty
    assert "azureuser@20.0.0.5" in cmd


def test_ensure_clone_script_is_idempotent_clone_or_fetch() -> None:
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    # The clone script is the final ssh argument (tty path passes it as the remote command).
    script = runner.calls[-1][-1]
    assert "git clone" in script
    assert "fetch --prune" in script
    assert "git@github.com:genshift/gswa-backend.git" in script
