"""Tests for GitSourceAccess — agent-forwarded clone, else fetch + safe fast-forward over SSH."""

import pytest

from billet.access.source.git_source_access import GitSourceAccess
from billet.contracts import WorkspaceSpec
from billet.shared.errors import ProcessError
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

SPEC = make_workspace_spec()
REMOTE = make_remote_host()


def _emitted_script(spec: WorkspaceSpec) -> str:
    """Return the remote bash GitSourceAccess emits (the final ssh argv element)."""
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(spec, REMOTE)
    # The tty path passes the whole script verbatim as the last ssh argument.
    return runner.calls[-1][-1]


def _first_git_invocation(script: str) -> int:
    """Offset of the first line that actually *runs* git (a comment mentioning it is not one)."""
    offset = 0
    for line in script.splitlines(keepends=True):
        if not line.lstrip().startswith("#") and "git " in line:
            return offset
        offset += len(line)
    raise AssertionError("the emitted script never invokes git")


def _guarded_failure_block(script: str, command: str) -> list[str]:
    """The ``if ! <command>`` guard's lines, through the ``exit 1`` that aborts on failure."""
    lines = script.splitlines()
    start = next(i for i, line in enumerate(lines) if f"if ! {command}" in line)
    end = next(i for i, line in enumerate(lines[start:], start) if line.strip() == "exit 1")
    return lines[start : end + 1]


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


def test_script_exports_the_non_interactive_env_before_any_git_runs() -> None:
    script = _emitted_script(SPEC)
    # `start` is unattended, so a git that CAN prompt hangs rather than fails. An export that
    # drifted below a git call would leave exactly that hang for the call above it.
    first_git = _first_git_invocation(script)
    assert script.index("export GIT_TERMINAL_PROMPT=0") < first_git
    assert script.index("export GIT_SSH_COMMAND=") < first_git
    ssh_command = next(
        line for line in script.splitlines() if line.startswith("export GIT_SSH_COMMAND=")
    )
    assert "BatchMode=yes" in ssh_command  # no passphrase / confirmation prompt from ssh
    assert "StrictHostKeyChecking=accept-new" in ssh_command  # trust-on-first-use preserved


def test_clone_failure_names_its_cause_and_aborts() -> None:
    block = _guarded_failure_block(_emitted_script(SPEC), "git clone")
    cause = next(line for line in block if "[billet/source]" in line)
    assert "clone failed" in cause
    assert ">&2" in cause  # a diagnostic, not progress output
    assert block[-1].strip() == "exit 1"


def test_fetch_failure_names_its_cause_and_aborts() -> None:
    block = _guarded_failure_block(_emitted_script(SPEC), "git fetch --prune")
    cause = next(line for line in block if "[billet/source]" in line)
    assert "fetch failed" in cause
    assert ">&2" in cause
    assert block[-1].strip() == "exit 1"  # ADR-0007: a fetch failure aborts start


def test_script_probes_origin_and_warns_on_drift_before_fetching() -> None:
    script = _emitted_script(SPEC)
    assert "git remote get-url origin" in script
    for reason in ("no 'origin' remote", "differs from the configured repo_url"):
        line = next(ln for ln in script.splitlines() if reason in ln)
        assert "[billet/source]" in line
    # The drift warning precedes the fetch failure it usually explains.
    assert script.index("git remote get-url origin") < script.index("git fetch --prune")


def test_script_never_rewrites_the_origin_remote() -> None:
    # An origin an operator repointed is adopted state (ADR-0005): billet reports, never edits.
    script = _emitted_script(SPEC)
    assert "remote set-url" not in script
    assert "remote add" not in script


def test_ensure_clone_raises_from_the_access_not_the_runner() -> None:
    # ensure_clone runs the ssh with check=False so it can build the error from BOTH streams.
    # FakeProcessRunner does not record the kwarg, so assert what it produces: a runner-raised
    # error (check=True) would carry the empty stderr, never the Host's output.
    runner = FakeProcessRunner(lambda _argv: completed(stdout="remote said no\n", returncode=1))
    with pytest.raises(ProcessError) as excinfo:
        GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    assert excinfo.value.stderr == "remote said no"


def test_ensure_clone_error_carries_the_pty_merged_stdout() -> None:
    # `ssh -t` gives the remote a pty, which merges its streams onto the CLIENT's stdout — so
    # the client's stderr is empty and a stderr-only report would render an empty tail.
    remote_output = (
        "[billet/source] fetch failed: git ran non-interactively\n"
        "fatal: could not read Username for 'https://github.com'"
    )
    runner = FakeProcessRunner(lambda _argv: completed(stdout=remote_output, returncode=128))
    with pytest.raises(ProcessError) as excinfo:
        GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    assert "[billet/source] fetch failed" in str(excinfo.value)
    assert "could not read Username" in str(excinfo.value)
    assert excinfo.value.returncode == 128


def test_ensure_clone_error_keeps_both_streams_in_the_order_produced() -> None:
    runner = FakeProcessRunner(
        lambda _argv: completed(stdout="host output\n", returncode=1, stderr="ssh noise\n")
    )
    with pytest.raises(ProcessError) as excinfo:
        GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    assert excinfo.value.stderr == "host output\nssh noise"


def test_ensure_clone_is_quiet_on_a_zero_exit() -> None:
    runner = FakeProcessRunner(lambda _argv: completed(stdout="[billet/source] up to date\n"))
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)  # no raise
    assert len(runner.calls) == 1
