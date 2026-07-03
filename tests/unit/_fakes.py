"""Shared in-memory fakes and spec factories for billet unit tests."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from billet.contracts import (
    DevcontainerFacts,
    HostPowerState,
    HostSpec,
    HostStatus,
    PlanStep,
    RemoteHost,
    WorkspacePlanStep,
    WorkspaceSpec,
)
from billet.infrastructure.process import CompletedProcess
from billet.shared.errors import ProcessError

_DEFAULT_HOST_SPEC = HostSpec(
    key="devbox",
    resource_group="gswa-devbox-rg",
    vm_name="gswa-devbox",
    location="westus3",
    admin_user="azureuser",
    vm_image="Canonical:image:latest",
    vm_size="Standard_D4s_v4",
    public_ip_sku="Standard",
    os_disk_gb=64,
    storage_sku="Premium_LRS",
    nsg_name="gswa-devboxNSG",
    ssh_rule_name="default-allow-ssh",
    manages_workspaces=True,
    docker_gpg_url="https://download.docker.com/linux/ubuntu/gpg",
    docker_apt_url="https://download.docker.com/linux/ubuntu",
)


def make_host_spec(**overrides: Any) -> HostSpec:
    """Return the canonical test HostSpec with any field overridden."""
    return replace(_DEFAULT_HOST_SPEC, **overrides)


_DEFAULT_WORKSPACE_SPEC = WorkspaceSpec(
    key="gswa-backend",
    host="devbox",
    repo_url="git@github.com:genshift/gswa-backend.git",
    repo_dir="gswa-backend",
    container_ssh_port=2222,
    host_alias="gswa-devbox",
    container_alias="gswa-container",
    tmux_session="main",
    agent_teams_flag="CLAUDE_CODE_EXPERIMENTAL_AGENT_TEAMS",
    host_bootstrap_cmd=":",
    verify_cmd="make test",
    status_color=None,
)

_DEFAULT_FACTS = DevcontainerFacts(
    service="gswa-backend",
    compose_files=(".devcontainer/docker-compose.yml",),
    workspace_folder="/app",
    remote_user="dev",
    post_create_command="bash .devcontainer/postcreate.sh",
)


def make_workspace_spec(**overrides: Any) -> WorkspaceSpec:
    """Return the canonical test WorkspaceSpec with any field overridden."""
    return replace(_DEFAULT_WORKSPACE_SPEC, **overrides)


def make_devcontainer_facts(**overrides: Any) -> DevcontainerFacts:
    """Return the canonical test DevcontainerFacts with any field overridden."""
    return replace(_DEFAULT_FACTS, **overrides)


def make_remote_host(admin_user: str = "azureuser", ip: str = "20.0.0.5") -> RemoteHost:
    """Return a RemoteHost for access/manager tests."""
    return RemoteHost(admin_user=admin_user, ip=ip)


def completed(stdout: str = "", returncode: int = 0, stderr: str = "") -> CompletedProcess:
    """Build a scripted CompletedProcess (argv is filled in by the runner)."""
    return CompletedProcess(argv=(), returncode=returncode, stdout=stdout, stderr=stderr)


class FakeProcessRunner:
    """Records argv (+ stdin) and returns scripted results from a handler keyed on argv."""

    def __init__(self, handler: Callable[[list[str]], CompletedProcess]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
    ) -> CompletedProcess:
        argv_list = list(argv)
        self.calls.append(tuple(argv_list))
        self.inputs.append(input_text)
        scripted = self._handler(argv_list)
        result = CompletedProcess(
            argv=tuple(argv_list),
            returncode=scripted.returncode,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
        )
        if check and result.returncode != 0:
            raise ProcessError(result.argv, result.returncode, result.stderr)
        return result

    def commands(self) -> list[str]:
        """Each recorded call joined into one string, for substring assertions."""
        return [" ".join(call) for call in self.calls]


class FakeHostProvider:
    """A HostProvider that records each call and returns a fixed status."""

    def __init__(self, status: HostStatus | None = None) -> None:
        self._status = status or HostStatus(HostPowerState.RUNNING, "1.2.3.4", "VM running")
        self.calls: list[str] = []

    def preflight(self) -> None:
        self.calls.append("preflight")

    def status(self, spec: HostSpec) -> HostStatus:
        self.calls.append("status")
        return self._status

    def create(self, spec: HostSpec) -> None:
        self.calls.append("create")

    def start(self, spec: HostSpec) -> None:
        self.calls.append("start")

    def deallocate(self, spec: HostSpec) -> None:
        self.calls.append("deallocate")

    def pin_inbound(self, spec: HostSpec) -> str:
        self.calls.append("pin_inbound")
        return "9.9.9.9/32"

    def wait_until_reachable(self, spec: HostSpec) -> None:
        self.calls.append("wait_until_reachable")

    def ensure_supply_chain(self, spec: HostSpec) -> None:
        self.calls.append("ensure_supply_chain")

    def ensure_tags(self, spec: HostSpec) -> None:
        self.calls.append("ensure_tags")


class RecordingPlanObserver:
    """A PlanObserver that records each ``(event, step)`` it receives, in order."""

    def __init__(self) -> None:
        self.events: list[tuple[str, PlanStep | WorkspacePlanStep]] = []

    def step_started(self, step: PlanStep | WorkspacePlanStep) -> None:
        self.events.append(("started", step))

    def step_succeeded(self, step: PlanStep | WorkspacePlanStep) -> None:
        self.events.append(("succeeded", step))

    def step_failed(self, step: PlanStep | WorkspacePlanStep) -> None:
        self.events.append(("failed", step))


class FakeSourceAccess:
    """A SourceAccess that records each clone request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ensure_clone(self, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        self.calls.append((spec.key, remote.ip))


class FakeContainerAccess:
    """A ContainerAccess that records calls and returns fixed facts / running state."""

    def __init__(self, facts: DevcontainerFacts | None = None, *, running: bool = True) -> None:
        self._facts = facts or _DEFAULT_FACTS
        self._running = running
        self.calls: list[str] = []
        self.personal_bootstrap_cmds: list[str] = []

    def read_facts(self, spec: WorkspaceSpec, remote: RemoteHost) -> DevcontainerFacts:
        self.calls.append("read_facts")
        return self._facts

    def compose_up(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> None:
        self.calls.append("compose_up")

    def run_post_create(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        self.calls.append("run_post_create")

    def run_personal_bootstrap(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts, command: str
    ) -> None:
        self.calls.append("run_personal_bootstrap")
        self.personal_bootstrap_cmds.append(command)

    def verify(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> None:
        self.calls.append("verify")

    def compose_stop(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        self.calls.append("compose_stop")

    def is_running(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> bool:
        self.calls.append("is_running")
        return self._running


class FakeSshConfigAccess:
    """An SshConfigAccess that captures the written content and Include calls."""

    def __init__(self) -> None:
        self.written: str | None = None
        self.include_calls = 0

    def write_conf(self, content: str) -> str:
        self.written = content
        return "/home/op/.ssh/config.d/billet.conf"

    def ensure_include(self) -> None:
        self.include_calls += 1
