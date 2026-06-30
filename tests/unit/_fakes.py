"""Shared in-memory fakes and spec factories for billet unit tests."""

from collections.abc import Callable, Sequence
from dataclasses import replace
from typing import Any

from billet.contracts import HostPowerState, HostSpec, HostStatus
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
