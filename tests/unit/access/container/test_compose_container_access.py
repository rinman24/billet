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
    CLAUDE_DIR_REPAIR,
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
        "docker compose -f .devcontainer/docker-compose.yml exec -T -u dev gswa-backend bash -c"
        in script
    )
    assert "exec python3 -" in script
    assert 'env["CLAUDE_CODE_OAUTH_TOKEN"] = token' in script
    assert ".claude" in script and "settings.json" in script
    # The token travels in the heredoc (STDIN) as a python literal, after the build step.
    assert "tok-secret-123" in script
    assert script.index("up -d --build") < script.index("python3 -")


def test_compose_up_repairs_the_claude_dir_in_the_same_exec_before_python3() -> None:
    # ADR-0013 §6: `up -d` returns before the Berth entrypoint has run its own repair, so
    # the injection re-owns a root-owned, empty ~/.claude itself — in the SAME exec session
    # as the write, ahead of `python3 -`, with the heredoc still on stdin.
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, REMOTE, FACTS, claude_oauth_token="tok-secret-123")
    script = runner.inputs[-1]
    assert script is not None
    lines = script.splitlines()
    opener = lines[_find_heredoc_opener(lines)]
    assert "install -d -o" in opener and "-m 0700" in opener
    assert opener.index("install -d") < opener.index("exec python3 -")
    assert opener.rstrip().endswith("<<'BILLET_CLAUDE_TOKEN_PY'")
    # Exactly one exec: the repair is not a separate compose call that could race the write.
    assert sum("docker compose" in line and " exec " in line for line in lines) == 1
    assert script.index("up -d --build") < script.index("install -d")


def _run_repair(home: Path, *, stat_uid: int, sudo_rc: int = 0) -> subprocess.CompletedProcess[str]:
    """Run ``CLAUDE_DIR_REPAIR`` under bash with ``stat`` and ``sudo`` faked on PATH.

    ``stat`` reports ``stat_uid`` as every path's owner so the uid-0 branch is reachable
    without root; ``sudo`` logs its argv to ``<home>/sudo.log`` and exits ``sudo_rc``
    instead of escalating. Everything else (``ls``, ``id``, ``getent``) is the real thing.
    """
    bin_dir = home / "fakebin"
    bin_dir.mkdir()
    (bin_dir / "stat").write_text(f"#!/bin/sh\necho {stat_uid}\n")
    # printf, not echo: dash (Ubuntu's /bin/sh) reads the leading `-n` of `sudo -n …` as
    # echo's own flag and drops it, which is exactly the argument the assertion is about.
    (bin_dir / "sudo").write_text(
        f'#!/bin/sh\nprintf \'%s\\n\' "$*" >> "$HOME/sudo.log"\nexit {sudo_rc}\n'
    )
    for tool in ("stat", "sudo"):
        (bin_dir / tool).chmod(0o755)
    return subprocess.run(
        ["bash", "-c", CLAUDE_DIR_REPAIR],
        text=True,
        capture_output=True,
        check=False,
        env={**os.environ, "HOME": str(home), "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"},
    )


def test_claude_dir_repair_reowns_a_root_owned_empty_dir(tmp_path: Path) -> None:
    # Cell 1 of ADR-0013: the daemon created the mountpoint root:root and nothing has
    # written to it yet — the one state the policy repairs.
    (tmp_path / ".claude").mkdir()
    result = _run_repair(tmp_path, stat_uid=0)
    assert result.returncode == 0, result.stderr
    uid, gid = os.getuid(), os.getgid()
    assert (tmp_path / "sudo.log").read_text() == (
        f"-n install -d -o {uid} -g {gid} -m 0700 {tmp_path}/.claude\n"
    )
    assert "[billet] repaired" in result.stdout


def test_claude_dir_repair_leaves_a_populated_root_owned_dir_alone(tmp_path: Path) -> None:
    # Populated root-owned is warn-only territory (never `chown -R`, never a re-own of a
    # directory someone has written into) — here the exec simply does not touch it and the
    # Python program's writability check produces the diagnostic.
    (tmp_path / ".claude").mkdir()
    (tmp_path / ".claude" / "settings.json").write_text("{}")
    result = _run_repair(tmp_path, stat_uid=0)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "sudo.log").exists()
    assert result.stdout == ""


def test_claude_dir_repair_leaves_a_dev_owned_dir_alone(tmp_path: Path) -> None:
    # Already the login user's: leave it, mode included — the common warm-start case.
    (tmp_path / ".claude").mkdir()
    result = _run_repair(tmp_path, stat_uid=1000)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "sudo.log").exists()


def test_claude_dir_repair_skips_when_the_dir_is_absent(tmp_path: Path) -> None:
    # No directory means no volume mounted there (or the Python program will mkdir it);
    # nothing to re-own.
    result = _run_repair(tmp_path, stat_uid=0)
    assert result.returncode == 0, result.stderr
    assert not (tmp_path / "sudo.log").exists()


def test_claude_dir_repair_failure_warns_and_does_not_fail_the_exec(tmp_path: Path) -> None:
    # PR #62 posture: a failed repair must not abort `start` on its own — the exec goes on
    # to `python3 -`, whose writability check names the real fault.
    (tmp_path / ".claude").mkdir()
    result = _run_repair(tmp_path, stat_uid=0, sudo_rc=1)
    assert result.returncode == 0
    assert "[billet] warning: repair of" in result.stderr
    assert "continuing" in result.stderr


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


@pytest.mark.skipif(os.geteuid() == 0, reason="root writes anywhere; the guard is invisible")
def test_merge_program_refuses_when_the_claude_dir_is_not_writable(tmp_path: Path) -> None:
    # F4: before ADR-0013 a root-owned ~/.claude slipped past the `not exists()` guard and
    # `tempfile.mkstemp` raised PermissionError outside the try — a traceback at COMPOSE_UP.
    # Now the program checks writability first and names the owner and the remedy.
    claude_dir = tmp_path / ".claude"
    claude_dir.mkdir(mode=0o500)
    try:
        result = _run_merge("tok-secret", tmp_path)
    finally:
        claude_dir.chmod(0o700)
    assert result.returncode == 1
    assert "Traceback" not in result.stderr
    assert "tok-secret" not in result.stderr
    assert result.stderr.startswith(f"[billet] refusing to write {claude_dir / 'settings.json'}")
    assert f"{claude_dir} is owned by uid {os.getuid()}, not writable by" in result.stderr
    assert "re-run billet start after the Berth 1 entrypoint is in place" in result.stderr
    assert not (claude_dir / "settings.json").exists()


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


def test_compose_up_exports_the_host_admin_users_authorized_keys_before_up() -> None:
    # The compose snippet mounts ${BILLET_AUTHORIZED_KEYS:-./authorized_keys-stub} onto the
    # container's authorized_keys. billet exports the path itself, from the admin user it is
    # already ssh'd in as, so no .env on the Host is needed for sshd to trust the operator's
    # key. Exported before `up`, where compose interpolates it.
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, make_remote_host(admin_user="opsadmin"), FACTS)
    script = runner.inputs[-1]
    assert script is not None
    export = "export BILLET_AUTHORIZED_KEYS=/home/opsadmin/.ssh/authorized_keys"
    assert export in script
    assert script.index(export) < script.index("up -d --build")
    # Beside its sibling, ahead of the host hook — the hook may itself call compose.
    assert script.index("export BILLET_CONTAINER_SSH_PORT") < script.index(export)
    assert script.index(export) < script.index('eval "$HOST_BOOTSTRAP_CMD"')


def test_every_compose_op_exports_the_authorized_keys_path() -> None:
    # Same reasoning as the port: every compose invocation interpolates the same file, and
    # a `stop` or `ps` that saw the variable unset would warn about the missing default.
    remote = make_remote_host(admin_user="opsadmin")
    access, runner = _access(lambda _argv: completed(stdout="abc\n"))
    access.compose_up(SPEC, remote, FACTS)
    access.run_post_create(SPEC, remote, FACTS)
    access.verify(SPEC, remote, FACTS)
    access.compose_stop(SPEC, remote, FACTS)
    access.is_running(SPEC, remote, FACTS)
    for script in runner.inputs:
        assert script is not None
        assert "export BILLET_AUTHORIZED_KEYS=/home/opsadmin/.ssh/authorized_keys" in script


def test_the_authorized_keys_export_is_shell_quoted() -> None:
    # The admin user comes from config.toml; an odd name must not break the script.
    access, runner = _access(lambda _argv: completed())
    access.compose_up(SPEC, make_remote_host(admin_user="ops admin"), FACTS)
    script = runner.inputs[-1]
    assert script is not None
    assert (
        f"export BILLET_AUTHORIZED_KEYS={shlex.quote('/home/ops admin/.ssh/authorized_keys')}"
        in script
    )


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


def test_verify_returns_what_the_command_printed() -> None:
    # The captured text is the point of the step: a version check whose output is thrown
    # away tells the operator nothing. The runner replays scripted stdout line by line
    # through the streaming sink, and verify joins it back with no trailing newline.
    printed = "pytest 8.3.2\nruff 0.6.9\nmypy 1.11.2"
    access, _ = _access(lambda _argv: completed(stdout=printed + "\n"))
    assert access.verify(SPEC, REMOTE, FACTS) == printed


def test_verify_failure_carries_the_merged_output_not_the_bare_stderr() -> None:
    # A verify_cmd is normally a test or build runner, and those report their verdict on
    # stdout — so the ProcessError the runner raises (stderr only) would render an empty
    # tail for the one outcome the operator most needs to read.
    printed = "FAILED tests/test_thing.py::test_it\n1 failed, 12 passed"
    access, _ = _access(lambda _argv: completed(stdout=printed + "\n", returncode=2, stderr=""))
    with pytest.raises(ProcessError) as excinfo:
        access.verify(SPEC, REMOTE, FACTS)
    assert excinfo.value.returncode == 2
    assert excinfo.value.stderr == printed


def test_verify_failure_falls_back_to_stderr_when_nothing_was_printed() -> None:
    # Nothing reached the sink (the shell itself failed before the command ran), so the
    # error keeps whatever the runner captured rather than replacing it with an empty tail.
    access, _ = _access(lambda _argv: completed(stdout="", returncode=1, stderr="bash: no such"))
    with pytest.raises(ProcessError) as excinfo:
        access.verify(SPEC, REMOTE, FACTS)
    assert excinfo.value.stderr == "bash: no such"


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


def test_only_compose_up_reaches_the_client_sink() -> None:
    # verify streams too, but into a private sink it owns and returns; nothing it prints
    # may reach the caller-injected sink, which is compose-up's log tail alone. compose_stop
    # does not stream at all.
    runner = FakeProcessRunner(lambda argv: completed(stdout="a line\n"))
    seen: list[str] = []
    access = ComposeContainerAccess(runner, on_compose_line=seen.append)
    access.compose_stop(SPEC, REMOTE, FACTS)
    access.verify(SPEC, REMOTE, FACTS)
    assert seen == []
    assert runner.streamed_calls == [1]  # compose_stop stayed buffered; verify streamed privately
    access.compose_up(SPEC, REMOTE, FACTS)
    assert seen == ["a line"]  # only compose_up feeds the client
