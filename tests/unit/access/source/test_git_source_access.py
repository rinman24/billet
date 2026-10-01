"""Tests for GitSourceAccess — agent-forwarded clone, else fetch + safe fast-forward over SSH."""

import pytest

from billet.access.source.git_source_access import GitSourceAccess
from billet.contracts import WorkspaceSpec
from billet.shared.errors import ProcessError
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host, make_workspace_spec

SPEC = make_workspace_spec()
REMOTE = make_remote_host()


def _emitted_script(spec: WorkspaceSpec) -> str:
    """Return the remote bash GitSourceAccess emits (fed to ``bash -se`` on stdin)."""
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(spec, REMOTE)
    script = runner.inputs[-1]
    assert script is not None, "the clone script must travel on stdin, not in the argv"
    return script


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


def test_ensure_clone_forwards_the_agent_in_batch_mode_without_a_pty() -> None:
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    argv = runner.calls[-1]
    assert argv[0] == "ssh"
    assert "-A" in argv  # agent forwarding — key stays on the operator's machine
    # No pty: `ssh -t` on the operator's terminal switches it to raw mode and breaks the
    # start checklist's in-place redraw (ADR-0007, amendment 2026-10-01).
    assert "-t" not in argv
    assert "-tt" not in argv
    assert "BatchMode=yes" in argv
    assert any(opt.startswith("ConnectTimeout=") for opt in argv)
    assert argv[-2:] == ("azureuser@20.0.0.5", "bash -se")


def test_ensure_clone_feeds_the_script_on_stdin() -> None:
    runner = FakeProcessRunner(lambda _argv: completed())
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    script = runner.inputs[-1]
    assert script is not None
    assert script.startswith("set -euo pipefail\n")
    # The script is not duplicated into the argv as a remote command.
    assert all("git clone" not in arg for arg in runner.calls[-1])


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
    # error (check=True) would carry only the empty stderr, never the Host's stdout.
    runner = FakeProcessRunner(lambda _argv: completed(stdout="remote said no\n", returncode=1))
    with pytest.raises(ProcessError) as excinfo:
        GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    assert excinfo.value.stderr == "remote said no"


def test_ensure_clone_error_carries_the_hosts_stderr() -> None:
    # Without a pty the Host's diagnostics — git's own error and the script's cause line —
    # arrive on the client's stderr, separate from stdout.
    diagnostics = (
        "fatal: could not read Username for 'https://github.com'\n"
        "[billet/source] fetch failed: git ran non-interactively"
    )
    runner = FakeProcessRunner(lambda _argv: completed(returncode=1, stderr=diagnostics))
    with pytest.raises(ProcessError) as excinfo:
        GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    assert "could not read Username" in str(excinfo.value)
    assert "[billet/source] fetch failed" in str(excinfo.value)
    assert excinfo.value.returncode == 1


def test_ensure_clone_error_puts_the_progress_before_the_diagnostics() -> None:
    # stdout holds the progress and drift warnings the script prints before the failing git
    # call; stderr holds the failure. A drift warning usually explains the failure, so it is
    # kept and comes first.
    progress = (
        "[billet/source] repo already present; fetching ...\n"
        "[billet/source] warning: origin fetch URL 'https://x' differs from the configured repo_url\n"
    )
    failure = "[billet/source] fetch failed: git ran non-interactively\n"
    runner = FakeProcessRunner(
        lambda _argv: completed(stdout=progress, returncode=1, stderr=failure)
    )
    with pytest.raises(ProcessError) as excinfo:
        GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)
    assert excinfo.value.stderr == progress + failure.rstrip("\n")


def test_ensure_clone_is_quiet_on_a_zero_exit() -> None:
    runner = FakeProcessRunner(lambda _argv: completed(stdout="[billet/source] up to date\n"))
    GitSourceAccess(runner).ensure_clone(SPEC, REMOTE)  # no raise
    assert len(runner.calls) == 1
