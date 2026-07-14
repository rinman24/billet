"""Tests for ComposeContainerAccess — devcontainer.json parsing + compose argv over SSH."""

import ast
from collections.abc import Callable
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys

import pytest

from billet.access.container.compose_container_access import (
    ComposeContainerAccess,
    build_claude_merge_program,
)
from billet.infrastructure.process import CompletedProcess
from billet.shared.errors import ConfigError, HostOperationError, ProcessError
from tests.unit._fakes import (
    FakeProcessRunner,
    completed,
    make_devcontainer_facts,
    make_remote_host,
    make_workspace_spec,
)

SPEC = make_workspace_spec()
REMOTE = make_remote_host()
FACTS = make_devcontainer_facts()

Handler = Callable[[list[str]], CompletedProcess]

_GSWA_DEVCONTAINER = """
{
    "name": "GenShift Development Container",
    "dockerComposeFile": "docker-compose.yml",  // relative to .devcontainer/
    "service": "gswa-backend",
    "workspaceFolder": "/app",
    "postCreateCommand": "bash .devcontainer/postcreate.sh",
    "remoteUser": "dev",
}
"""


def _access(handler: Handler) -> tuple[ComposeContainerAccess, FakeProcessRunner]:
    runner = FakeProcessRunner(handler)
    return ComposeContainerAccess(runner), runner


# --- read_facts --------------------------------------------------------------------


def test_read_facts_parses_gswa_devcontainer() -> None:
    access, runner = _access(lambda _argv: completed(stdout=_GSWA_DEVCONTAINER))
    facts = access.read_facts(SPEC, REMOTE)
    assert facts.service == "gswa-backend"
    assert facts.compose_files == (".devcontainer/docker-compose.yml",)
    assert facts.workspace_folder == "/app"
    assert facts.remote_user == "dev"
    assert facts.post_create_command == "bash .devcontainer/postcreate.sh"
    # Reads the file over SSH from the host's repo checkout.
    assert runner.commands()[-1].endswith("cat gswa-backend/.devcontainer/devcontainer.json")


def test_read_facts_normalizes_list_compose_files() -> None:
    text = (
        '{"dockerComposeFile": ["docker-compose.yml", "compose.override.yml"], '
        '"service": "s", "workspaceFolder": "/app", "remoteUser": "dev"}'
    )
    access, _ = _access(lambda _argv: completed(stdout=text))
    facts = access.read_facts(SPEC, REMOTE)
    assert facts.compose_files == (
        ".devcontainer/docker-compose.yml",
        ".devcontainer/compose.override.yml",
    )


def test_read_facts_normalizes_list_post_create() -> None:
    text = (
        '{"dockerComposeFile": "docker-compose.yml", "service": "s", '
        '"workspaceFolder": "/app", "remoteUser": "dev", '
        '"postCreateCommand": ["make", "install"]}'
    )
    access, _ = _access(lambda _argv: completed(stdout=text))
    facts = access.read_facts(SPEC, REMOTE)
    assert facts.post_create_command == "make install"


def test_read_facts_raises_when_file_unreadable() -> None:
    access, _ = _access(lambda _argv: completed(returncode=1, stderr="No such file"))
    with pytest.raises(ConfigError, match="could not read"):
        access.read_facts(SPEC, REMOTE)


def test_read_facts_raises_host_error_when_ssh_cannot_connect() -> None:
    # ssh exits 255 for its own failures — a deallocated host, not a missing repo.
    access, _ = _access(lambda _argv: completed(returncode=255, stderr="Connection timed out"))
    with pytest.raises(HostOperationError, match="could not reach"):
        access.read_facts(SPEC, REMOTE)


def test_read_facts_raises_on_missing_service() -> None:
    text = '{"dockerComposeFile": "docker-compose.yml", "workspaceFolder": "/app", "remoteUser": "dev"}'
    access, _ = _access(lambda _argv: completed(stdout=text))
    with pytest.raises(ConfigError, match="'service'"):
        access.read_facts(SPEC, REMOTE)


def test_read_facts_raises_on_object_post_create() -> None:
    text = (
        '{"dockerComposeFile": "docker-compose.yml", "service": "s", '
        '"workspaceFolder": "/app", "remoteUser": "dev", '
        '"postCreateCommand": {"a": "x", "b": "y"}}'
    )
    access, _ = _access(lambda _argv: completed(stdout=text))
    with pytest.raises(ConfigError, match="object form"):
        access.read_facts(SPEC, REMOTE)


# --- driving compose ---------------------------------------------------------------


def test_compose_up_runs_build_with_host_hook_and_agent_teams() -> None:
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert "docker compose -f .devcontainer/docker-compose.yml up -d --build" in script
    assert 'eval "$HOST_BOOTSTRAP_CMD"' in script
    assert ".claude/settings.local.json" in script
    # Runs non-interactively over `bash -se` on the host.
    assert runner.commands()[-1].endswith("bash -se")


def test_compose_up_without_token_emits_no_injection() -> None:
    # Backward compat: no token ⇒ no python3 exec, no settings write — script unchanged.
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert "python3" not in script
    assert "CLAUDE_CODE_OAUTH_TOKEN" not in script
    assert "settings.json" not in script


def test_compose_up_injects_token_via_python3_exec_over_stdin() -> None:
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS, claude_oauth_token="tok-secret-123")
    script = runner.inputs[-1]
    assert script is not None
    # The merge runs as the container login user, targeting settings.json via python3.
    assert (
        "docker compose -f .devcontainer/docker-compose.yml exec -T -u dev gswa-backend python3 -"
        in script
    )
    assert 'env["CLAUDE_CODE_OAUTH_TOKEN"] = token' in script
    assert ".claude" in script and "settings.json" in script
    # The token travels in the heredoc (STDIN) as a python literal, after the build step.
    assert "tok-secret-123" in script
    assert script.index("up -d --build") < script.index("python3 -")


def _find_heredoc_opener(lines: list[str]) -> int:
    """Index of the `docker compose … exec … python3 -` line that opens the heredoc."""
    return next(
        i
        for i, line in enumerate(lines)
        if "docker compose" in line and "exec" in line and "python3" in line
    )


def test_compose_up_keeps_token_out_of_every_argv() -> None:
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS, claude_oauth_token="tok-secret-123")
    # (1) Outer argv: the token never rides the recorded `ssh … bash -se` command line.
    for command in runner.commands():
        assert "tok-secret-123" not in command
    # (2) Inside the generated SCRIPT, the token must appear ONLY on heredoc-body lines —
    # never on the `docker compose … exec … python3 -` line that opens the heredoc, nor any
    # line before it (those are argv/shell, visible via ps/proc). This is the assertion the
    # trivial outer-argv check gave false confidence about: it would now fail if the token
    # were moved onto the exec command line.
    script = runner.inputs[-1]
    assert script is not None
    lines = script.splitlines()
    opener_idx = _find_heredoc_opener(lines)
    for line in lines[: opener_idx + 1]:
        assert "tok-secret-123" not in line
    assert any("tok-secret-123" in line for line in lines[opener_idx + 1 :])


def test_compose_up_hostile_token_cannot_break_out_of_the_heredoc() -> None:
    # A token engineered to break a naive shell/heredoc scheme: embedded quotes, a `$`, a
    # backtick, a real newline, a line that is *exactly* the heredoc terminator, and a
    # trailing backslash. repr() + a single-quoted heredoc must survive all of it.
    nasty_token = "a'b\"c\\d$e`f\nBILLET_CLAUDE_TOKEN_PY\n\\"
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS, claude_oauth_token=nasty_token)
    script = runner.inputs[-1]
    assert script is not None
    lines = script.splitlines()
    # (a) Exactly one bare terminator line — the real one. The token's embedded copy stays
    # inside the repr() literal (escaped, on one line), so it cannot prematurely close the
    # heredoc and let the shell interpret the tail.
    assert lines.count("BILLET_CLAUDE_TOKEN_PY") == 1
    # (b) The `token = <repr>` literal round-trips back to the exact original bytes.
    token_line = next(line for line in lines if line.startswith("token = "))
    assert ast.literal_eval(token_line[len("token = ") :]) == nasty_token


# --- the in-container merge program, executed end-to-end -----------------------------


def _run_merge(token: str, home: Path) -> subprocess.CompletedProcess[str]:
    """Run the assembled merge program under a real python3 with HOME pointed at ``home``."""
    program = build_claude_merge_program(token)
    return subprocess.run(
        [sys.executable, "-"],
        input=program,
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "HOME": str(home)},
    )


def test_merge_program_preserves_existing_settings(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text(
        json.dumps({"model": "x", "env": {"FOO": "1"}, "permissions": {"allow": ["Bash"]}})
    )
    result = _run_merge("tok-sample-xyz", tmp_path)
    assert result.returncode == 0, result.stderr
    data = json.loads(settings.read_text())
    # Foreign keys and the pre-existing env entry all survive the merge.
    assert data["model"] == "x"
    assert data["permissions"] == {"allow": ["Bash"]}
    assert data["env"]["FOO"] == "1"
    assert data["env"]["CLAUDE_CODE_OAUTH_TOKEN"] == "tok-sample-xyz"
    assert (settings.stat().st_mode & 0o777) == 0o600


def test_merge_program_creates_file_when_absent(tmp_path: Path) -> None:
    result = _run_merge("tok-new", tmp_path)
    assert result.returncode == 0, result.stderr
    settings = tmp_path / ".claude" / "settings.json"
    assert json.loads(settings.read_text()) == {"env": {"CLAUDE_CODE_OAUTH_TOKEN": "tok-new"}}
    assert (settings.stat().st_mode & 0o777) == 0o600
    # Fix 7: a billet-created ~/.claude is locked down to the owner.
    assert ((tmp_path / ".claude").stat().st_mode & 0o777) == 0o700


def test_merge_program_refuses_to_clobber_invalid_json(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text("{not valid json")
    result = _run_merge("tok-secret", tmp_path)
    assert result.returncode != 0
    assert "tok-secret" not in result.stderr  # the diagnostic never leaks the token
    assert settings.read_text() == "{not valid json"  # left untouched for the operator


def test_merge_program_refuses_when_top_level_is_not_an_object(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    settings.write_text("[1, 2, 3]")
    result = _run_merge("tok-secret", tmp_path)
    assert result.returncode != 0
    assert "tok-secret" not in result.stderr
    assert settings.read_text() == "[1, 2, 3]"


def test_merge_program_refuses_when_env_is_not_an_object(tmp_path: Path) -> None:
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir()
    settings = claude_dir / "settings.json"
    original = json.dumps({"model": "x", "env": "not-a-dict"})
    settings.write_text(original)
    result = _run_merge("tok-secret", tmp_path)
    assert result.returncode != 0
    assert "tok-secret" not in result.stderr
    assert settings.read_text() == original  # env is not silently replaced


def test_compose_up_does_not_disturb_the_agent_teams_block() -> None:
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS, claude_oauth_token="tok-secret-123")
    script = runner.inputs[-1]
    assert script is not None
    # The separate host-side agent-teams settings.local.json write is untouched.
    assert ".claude/settings.local.json" in script
    assert '"$AGENT_TEAMS_FLAG": "1"' in script


def test_compose_up_exports_the_assigned_loopback_port() -> None:
    # The repo's compose binds sshd to ${BILLET_CONTAINER_SSH_PORT} (ADR-0003).
    access, runner = _access(lambda _argv: completed())
    access.compose_up(make_workspace_spec(container_ssh_port=2223), REMOTE, FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert "export BILLET_CONTAINER_SSH_PORT=2223" in script


def test_every_compose_op_exports_the_port() -> None:
    # The personal bootstrap is absent here: it hops through the container's sshd rather
    # than driving compose, so it targets the port directly (asserted in its own tests).
    spec = make_workspace_spec(container_ssh_port=2299)
    access, runner = _access(lambda _argv: completed(stdout="abc\n"))
    access.compose_up(spec, REMOTE, FACTS)
    access.run_post_create(spec, REMOTE, FACTS)
    access.verify(spec, REMOTE, FACTS)
    access.compose_stop(spec, REMOTE, FACTS)
    access.is_running(spec, REMOTE, FACTS)
    for script in runner.inputs:
        assert script is not None
        assert "export BILLET_CONTAINER_SSH_PORT=2299" in script


def test_run_post_create_execs_in_service_container() -> None:
    access, runner = _access(lambda _argv: completed())
    access.run_post_create(SPEC, REMOTE, FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert "exec -T gswa-backend bash -lc 'bash .devcontainer/postcreate.sh'" in script


def test_run_post_create_is_a_noop_when_absent() -> None:
    access, runner = _access(lambda _argv: completed())
    access.run_post_create(SPEC, REMOTE, make_devcontainer_facts(post_create_command=None))
    assert runner.calls == []


def test_run_personal_bootstrap_forwards_the_agent_to_the_host() -> None:
    access, runner = _access(lambda _argv: completed())
    access.run_personal_bootstrap(SPEC, REMOTE, FACTS, "bash ~/dotfiles/install.sh")
    argv = runner.calls[-1]
    # Outer hop to the Host: agent-forwarded, non-interactive, feeding a `bash -se` script.
    assert "-A" in argv
    assert "BatchMode=yes" in argv
    # Trust-on-first-use stays on for the Host hop; only the loopback hop disables it.
    assert "StrictHostKeyChecking=accept-new" in argv
    assert argv[-2:] == ("azureuser@20.0.0.5", "bash -se")


def test_run_personal_bootstrap_hops_to_the_container_sshd() -> None:
    access, runner = _access(lambda _argv: completed())
    spec = make_workspace_spec(container_ssh_port=2299)
    access.run_personal_bootstrap(spec, REMOTE, FACTS, "bash ~/dotfiles/install.sh")
    script = runner.inputs[-1]
    assert script is not None
    # Inner hop: agent-forwarded again, to the container's loopback sshd on the assigned
    # port (ADR-0003) as the devcontainer's remoteUser.
    assert "ssh -n -A" in script
    assert "-p 2299" in script
    assert "dev@127.0.0.1" in script


def test_run_personal_bootstrap_double_quotes_the_command() -> None:
    access, runner = _access(lambda _argv: completed())
    command = "git clone git@github.com:me/dotfiles ~/dotfiles && bash ~/dotfiles/install.sh"
    access.run_personal_bootstrap(SPEC, REMOTE, FACTS, command)
    script = runner.inputs[-1]
    assert script is not None
    # Two shells each consume one quoting layer (host bash parses the hop line, then the
    # container-side sshd shell evaluates the remote command), leaving `cd <workspaceFolder>
    # && <command>` as the single bash -lc argument in the container.
    assert shlex.quote(shlex.quote(f"cd /app && {command}")) in script


def test_run_personal_bootstrap_keeps_embedded_quotes_literal() -> None:
    access, runner = _access(lambda _argv: completed())
    command = "echo 'hi' && cd $HOME"
    access.run_personal_bootstrap(SPEC, REMOTE, FACTS, command)
    script = runner.inputs[-1]
    assert script is not None
    # The double shlex.quote keeps quotes and $HOME literal through both shells.
    assert shlex.quote(shlex.quote(f"cd /app && {command}")) in script


def test_run_personal_bootstrap_disables_host_key_checks_for_the_loopback_hop() -> None:
    access, runner = _access(lambda _argv: completed())
    access.run_personal_bootstrap(SPEC, REMOTE, FACTS, "bash ~/dotfiles/install.sh")
    script = runner.inputs[-1]
    assert script is not None
    # The container regenerates host keys on rebuild and the hop never leaves the VM's
    # loopback, so host-key checking is disabled for this hop only.
    assert "StrictHostKeyChecking=no" in script
    assert "UserKnownHostsFile=/dev/null" in script


def test_run_personal_bootstrap_is_a_noop_when_empty() -> None:
    access, runner = _access(lambda _argv: completed())
    access.run_personal_bootstrap(SPEC, REMOTE, FACTS, "")
    assert runner.calls == []


def test_run_personal_bootstrap_failure_propagates() -> None:
    access, _ = _access(lambda _argv: completed(returncode=1, stderr="install failed"))
    with pytest.raises(ProcessError):
        access.run_personal_bootstrap(SPEC, REMOTE, FACTS, "bash ~/dotfiles/install.sh")


def test_verify_execs_verify_cmd_in_service_container() -> None:
    access, runner = _access(lambda _argv: completed())
    access.verify(SPEC, REMOTE, FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert "exec -T gswa-backend bash -lc 'make test'" in script


def test_compose_stop_is_non_destructive() -> None:
    access, runner = _access(lambda _argv: completed())
    access.compose_stop(SPEC, REMOTE, FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert "docker compose -f .devcontainer/docker-compose.yml stop" in script
    assert "down" not in script  # never tears down volumes


def test_is_running_true_when_ps_returns_a_container_id() -> None:
    access, _ = _access(lambda _argv: completed(stdout="abc123\n"))
    assert access.is_running(SPEC, REMOTE, FACTS) is True


def test_is_running_false_when_ps_empty() -> None:
    access, _ = _access(lambda _argv: completed(stdout=""))
    assert access.is_running(SPEC, REMOTE, FACTS) is False


def test_is_running_raises_host_error_when_ssh_cannot_connect() -> None:
    access, _ = _access(lambda _argv: completed(returncode=255, stderr="Connection timed out"))
    with pytest.raises(HostOperationError, match="could not reach"):
        access.is_running(SPEC, REMOTE, FACTS)


def test_every_ssh_call_bounds_connection_establishment() -> None:
    # A deallocated Azure host drops packets: without ConnectTimeout, probes hang forever.
    access, runner = _access(lambda _argv: completed(stdout=_GSWA_DEVCONTAINER))
    access.read_facts(SPEC, REMOTE)
    access.is_running(SPEC, REMOTE, FACTS)
    access.compose_stop(SPEC, REMOTE, FACTS)
    access.run_personal_bootstrap(SPEC, REMOTE, FACTS, "bash ~/dotfiles/install.sh")
    for command in runner.commands():
        assert "ConnectTimeout=5" in command


def test_compose_up_streams_through_the_constructor_sink() -> None:
    runner = FakeProcessRunner(lambda argv: completed(stdout="#5 [2/7] RUN pip install\n"))
    seen: list[str] = []
    access = ComposeContainerAccess(runner, on_compose_line=seen.append)
    access.compose_up(SPEC, REMOTE, FACTS)
    assert runner.streamed_calls == [0]  # the compose-up call streamed
    assert seen == ["#5 [2/7] RUN pip install"]


def test_only_compose_up_streams() -> None:
    runner = FakeProcessRunner(lambda argv: completed(stdout=""))
    access = ComposeContainerAccess(runner, on_compose_line=lambda line: None)
    access.compose_stop(SPEC, REMOTE, FACTS)
    access.verify(SPEC, REMOTE, FACTS)
    assert runner.streamed_calls == []  # every other operation stays buffered
