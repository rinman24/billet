"""Tests for ComposeContainerAccess — devcontainer.json parsing + compose argv over SSH."""

from collections.abc import Callable
import shlex

import pytest

from billet.access.container.compose_container_access import ComposeContainerAccess
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
