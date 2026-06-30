"""Host data contracts and the ``HostProvider`` service Protocol (the backend seam).

``HostProvider`` is the single seam that absorbs cloud-backend volatility (Azure VM today;
DevPod / Dev Box later). It lives here — below ``access`` in the layer graph — so the
``access`` implementation and the ``host`` manager both depend on the abstraction without
an upward import. See ADR-0001.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


class HostPowerState(Enum):
    """Normalized host power state, mapped from the backend's raw value."""

    NOTEXIST = "notexist"
    RUNNING = "running"
    DEALLOCATED = "deallocated"
    STOPPED = "stopped"
    OTHER = "other"


@dataclass(frozen=True, slots=True)
class HostSpec:
    """Operator intent for one Host, parsed from a ``[hosts.<key>]`` table."""

    key: str
    resource_group: str
    vm_name: str
    location: str
    admin_user: str
    vm_image: str
    vm_size: str
    public_ip_sku: str
    os_disk_gb: int
    storage_sku: str
    nsg_name: str
    ssh_rule_name: str
    manages_workspaces: bool
    docker_gpg_url: str
    docker_apt_url: str


@dataclass(frozen=True, slots=True)
class HostStatus:
    """Live host state derived from the backend."""

    power_state: HostPowerState
    public_ip: str | None
    raw_power: str


class HostProvider(Protocol):
    """The cloud-backend seam: everything that differs Azure VM -> DevPod -> Dev Box."""

    def preflight(self) -> None:
        """Verify backend auth and pin ambient state; raise with remediation if absent."""
        ...

    def status(self, spec: HostSpec) -> HostStatus:
        """Return the host's power state and public IP."""
        ...

    def create(self, spec: HostSpec) -> None:
        """Cold-provision the host. BILLABLE — gated by the client before apply."""
        ...

    def start(self, spec: HostSpec) -> None:
        """Resume a deallocated host."""
        ...

    def deallocate(self, spec: HostSpec) -> None:
        """Deallocate the host (stops compute billing; the OS disk persists)."""
        ...

    def pin_inbound(self, spec: HostSpec) -> str:
        """Re-pin the inbound SSH rule to the operator's current ``/32``; return the CIDR."""
        ...

    def wait_until_reachable(self, spec: HostSpec) -> None:
        """Block until the host answers SSH, or raise after exhausting attempts."""
        ...

    def ensure_supply_chain(self, spec: HostSpec) -> None:
        """Idempotently install the base supply chain (Docker) on the host."""
        ...
