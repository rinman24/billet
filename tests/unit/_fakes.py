"""Shared in-memory fakes and spec factories for billet unit tests."""

from collections.abc import Callable, Mapping, Sequence
from dataclasses import replace
from pathlib import Path
from typing import Any

from billet.contracts import (
    BERTH_COPIED_FILES,
    ComposeContainer,
    ContainerMetrics,
    CpuMetrics,
    DevcontainerFacts,
    DiskMetrics,
    HostMetrics,
    HostPowerState,
    HostSpec,
    HostStatus,
    MemoryMetrics,
    PackagedBerth,
    PlanStep,
    PortPublisher,
    ProvisioningSpec,
    RemoteHost,
    RuntimeState,
    WorkspaceBerthRead,
    WorkspacePlanStep,
    WorkspaceProbe,
    WorkspaceRuntimeRead,
    WorkspaceSpec,
)
from billet.infrastructure.process import CompletedProcess
from billet.shared.errors import (
    ConfigError,
    HostOperationError,
    ProcessError,
    ProcessTimeoutError,
)

_DEFAULT_HOST_SPEC = HostSpec(
    key="devbox",
    resource_group="rg-gswa-devbox",
    vm_name="gswa-devbox",
    location="westus3",
    admin_user="azureuser",
    provisioning=ProvisioningSpec(
        vm_image="Canonical:image:latest",
        vm_size="Standard_D4s_v4",
        public_ip_sku="Standard",
        os_disk_gb=64,
        storage_sku="Premium_LRS",
    ),
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
    """Records argv (+ stdin) and returns scripted results from a handler keyed on argv.

    When a caller streams (``on_line``), the scripted stdout is replayed through the
    callback line by line, and the call's index is recorded in ``streamed_calls``.
    """

    def __init__(self, handler: Callable[[list[str]], CompletedProcess]) -> None:
        self._handler = handler
        self.calls: list[tuple[str, ...]] = []
        self.inputs: list[str | None] = []
        self.timeouts: list[float | None] = []
        self.streamed_calls: list[int] = []

    def run(
        self,
        argv: Sequence[str],
        *,
        input_text: str | None = None,
        check: bool = True,
        on_line: Callable[[str], None] | None = None,
        timeout: float | None = None,
    ) -> CompletedProcess:
        argv_list = list(argv)
        self.calls.append(tuple(argv_list))
        self.inputs.append(input_text)
        self.timeouts.append(timeout)
        scripted = self._handler(argv_list)
        if on_line is not None:
            self.streamed_calls.append(len(self.calls) - 1)
            for line in scripted.stdout.splitlines():
                on_line(line)
        result = CompletedProcess(
            argv=tuple(argv_list),
            returncode=scripted.returncode,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
        )
        if check and result.returncode != 0:
            raise ProcessError(result.argv, result.returncode, result.stderr)
        return result

    def converse(
        self,
        argv: Sequence[str],
        *,
        opening: str,
        sentinel: str,
        reply: Callable[[str], str],
        timeout: float | None = None,
    ) -> CompletedProcess:
        """Replay the scripted stdout: the part up to ``sentinel`` feeds ``reply``.

        The call is recorded once, like ``run``; its input is the opening plus the reply.
        A handler that raises (e.g. :class:`ProcessTimeoutError`) raises from here.
        """
        argv_list = list(argv)
        self.calls.append(tuple(argv_list))
        self.timeouts.append(timeout)
        scripted = self._handler(argv_list)
        head: list[str] = []
        second = ""
        for line in scripted.stdout.splitlines(keepends=True):
            head.append(line)
            if line.rstrip("\n") == sentinel:
                second = reply("".join(head))
                break
        self.inputs.append(opening + second)
        return CompletedProcess(
            argv=tuple(argv_list),
            returncode=scripted.returncode,
            stdout=scripted.stdout,
            stderr=scripted.stderr,
        )

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
        self.outputs: list[str] = []

    def step_started(self, step: PlanStep | WorkspacePlanStep) -> None:
        self.events.append(("started", step))

    def step_succeeded(self, step: PlanStep | WorkspacePlanStep) -> None:
        self.events.append(("succeeded", step))

    def step_failed(self, step: PlanStep | WorkspacePlanStep) -> None:
        self.events.append(("failed", step))

    def step_output(self, step: PlanStep | WorkspacePlanStep, text: str) -> None:
        self.events.append(("output", step))
        self.outputs.append(text)


_DEFAULT_HOST_METRICS = HostMetrics(
    cpu=CpuMetrics(cores=4, load_1m=0.42, load_5m=0.31, load_15m=0.20),
    memory=MemoryMetrics(total_bytes=16 * 2**30, available_bytes=12 * 2**30),
    disks=(
        DiskMetrics(
            mount="/",
            size_bytes=64 * 2**30,
            used_bytes=46 * 2**30,
            available_bytes=18 * 2**30,
        ),
    ),
    containers=(
        ContainerMetrics(
            name="gswa-backend",
            status="Up 3 hours",
            cpu_percent="0.15%",
            mem_usage="1.2GiB / 15.6GiB",
            mem_percent="7.7%",
        ),
    ),
)


def make_host_metrics(**overrides: Any) -> HostMetrics:
    """Return the canonical test HostMetrics with any field overridden."""
    return replace(_DEFAULT_HOST_METRICS, **overrides)


class FakeMetricsAccess:
    """A MetricsAccess that records each probed remote and returns fixed metrics."""

    def __init__(self, metrics: HostMetrics | None = None) -> None:
        self._metrics = metrics or _DEFAULT_HOST_METRICS
        self.remotes: list[RemoteHost] = []

    def read(self, remote: RemoteHost) -> HostMetrics:
        self.remotes.append(remote)
        return self._metrics


class FakeSourceAccess:
    """A SourceAccess that records each clone request."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, str]] = []

    def ensure_clone(self, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        self.calls.append((spec.key, remote.ip))


class FakeContainerAccess:
    """A ContainerAccess that records calls and returns fixed facts / running state.

    ``uncloned`` and ``unreachable`` name the Workspace keys whose ``read_facts`` fails the
    way the real access does when the repo has not been cloned onto the Host yet
    (``ConfigError``) or the Host cannot be reached over SSH (``HostOperationError``);
    every other key keeps returning ``facts``.

    ``verify_output`` is what ``verify`` reports as the ``verify_cmd``'s captured output
    (empty by default, as most tests care only that the step ran).
    """

    def __init__(  # noqa: PLR0913 — a fake's knobs are all optional and independent
        self,
        facts: DevcontainerFacts | None = None,
        *,
        running: bool = True,
        uncloned: Sequence[str] = (),
        unreachable: Sequence[str] = (),
        verify_output: str = "",
    ) -> None:
        self._facts = facts or _DEFAULT_FACTS
        self._running = running
        self._uncloned = frozenset(uncloned)
        self._unreachable = frozenset(unreachable)
        self.verify_output = verify_output
        self.calls: list[str] = []
        self.personal_bootstrap_cmds: list[str] = []
        self.claude_oauth_tokens: list[str | None] = []

    def read_facts(self, spec: WorkspaceSpec, remote: RemoteHost) -> DevcontainerFacts:
        self.calls.append("read_facts")
        if spec.key in self._unreachable:
            raise HostOperationError(
                f"could not reach {remote.ip} over SSH — is the Host up? "
                "Run `billet host up` to start it."
            )
        if spec.key in self._uncloned:
            raise ConfigError(
                f"could not read {spec.repo_dir}/.devcontainer/devcontainer.json on "
                f"{remote.ip} — is the repo cloned? Run `billet start` to clone it first."
            )
        return self._facts

    def compose_up(
        self,
        spec: WorkspaceSpec,
        remote: RemoteHost,
        facts: DevcontainerFacts,
        claude_oauth_token: str | None = None,
    ) -> None:
        self.calls.append("compose_up")
        self.claude_oauth_tokens.append(claude_oauth_token)

    def run_post_create(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        self.calls.append("run_post_create")

    def run_personal_bootstrap(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts, command: str
    ) -> None:
        self.calls.append("run_personal_bootstrap")
        self.personal_bootstrap_cmds.append(command)

    def verify(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> str:
        self.calls.append("verify")
        return self.verify_output

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


# --- doctor ----------------------------------------------------------------------------

#: billet's own templates, used as *test fixtures* for the shipped Berth. Product code
#: never reads this path; it resolves the packaged copy (tests/unit/test_packaging.py).
TEMPLATE_DIR = Path(__file__).resolve().parents[2] / "templates" / "workspace"


def make_packaged_berth(version: int | None = None) -> PackagedBerth:
    """Return the repo's templates as a PackagedBerth (optionally with another version)."""
    files = {name: (TEMPLATE_DIR / name).read_text() for name in BERTH_COPIED_FILES}
    shipped = int(files["berth.version"].strip()) if version is None else version
    return PackagedBerth(version=shipped, files=files)


def make_berth_read(
    key: str = "gswa-backend",
    head: str | None = "d223cd5",
    overrides: Mapping[str, str | None] | None = None,
) -> WorkspaceBerthRead:
    """Return a read whose files equal the shipped templates, with ``overrides`` applied."""
    files: dict[str, str | None] = dict(make_packaged_berth().files)
    files.update(overrides or {})
    return WorkspaceBerthRead(workspace=key, head=head, files=files)


def make_container(
    project: str = "gswa-backend",
    service: str | None = None,
    *publishers: PortPublisher,
    container_id: str | None = None,
) -> ComposeContainer:
    """Return one running container of ``project``; ``service`` defaults to the project.

    With no ``publishers`` it publishes sshd on loopback, as every Workspace does (ADR-0003).
    """
    name = service or project
    return ComposeContainer(
        id=container_id or f"{project}-{name}",
        project=project,
        service=name,
        publishers=publishers or (PortPublisher("127.0.0.1", 22, 2222, "tcp"),),
    )


def make_runtime_read(
    *log_lines: str, project: str = "gswa-backend", containers: Sequence[ComposeContainer] = ()
) -> WorkspaceRuntimeRead:
    """Return a running container's read; with no lines, a clean start on the shipped Berth.

    ``containers`` are what the project ``ps`` lists; by default the one main-service
    container of ``project``, publishing sshd on loopback.
    """
    lines = log_lines or (f"dev-entrypoint: berth={make_packaged_berth().version}",)
    listed = tuple(containers) or (make_container(project),)
    return WorkspaceRuntimeRead(RuntimeState.RUNNING, log_lines=lines, containers=listed)


class FakeDoctorAccess:
    """A DoctorAccess that records each probed Host and returns scripted probes.

    ``unreachable`` / ``failing`` / ``timing_out`` name Host *ips* whose probe raises the way
    the real access does (``HostOperationError`` for an SSH transport failure,
    ``ProcessTimeoutError`` past the 30 s deadline, ``ProcessError`` otherwise).
    ``overrides`` maps a Workspace key to the file overrides its read carries; ``runtimes``
    maps a Workspace key to its runtime read (default: running, clean, on the shipped Berth).
    """

    def __init__(
        self,
        *,
        unreachable: Sequence[str] = (),
        failing: Sequence[str] = (),
        timing_out: Sequence[str] = (),
        overrides: Mapping[str, Mapping[str, str | None]] | None = None,
        runtimes: Mapping[str, WorkspaceRuntimeRead] | None = None,
    ) -> None:
        self._unreachable = frozenset(unreachable)
        self._failing = frozenset(failing)
        self._timing_out = frozenset(timing_out)
        self._overrides = overrides or {}
        self._runtimes = runtimes or {}
        self.calls: list[tuple[str, tuple[str, ...]]] = []

    def probe(
        self, remote: RemoteHost, specs: Sequence[WorkspaceSpec]
    ) -> tuple[WorkspaceProbe, ...]:
        self.calls.append((remote.ip, tuple(spec.key for spec in specs)))
        if remote.ip in self._unreachable:
            raise HostOperationError(f"could not reach {remote.ip} over SSH")
        if remote.ip in self._failing:
            raise ProcessError(["ssh", remote.ip, "bash -se"], 1, "bash: boom")
        if remote.ip in self._timing_out:
            raise ProcessTimeoutError(["ssh", remote.ip, "bash -se"], 30)
        return tuple(
            WorkspaceProbe(
                berth=make_berth_read(spec.key, overrides=self._overrides.get(spec.key)),
                runtime=self._runtimes.get(spec.key, make_runtime_read(project=spec.key)),
            )
            for spec in specs
        )
