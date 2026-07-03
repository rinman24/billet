"""Live host-usage contracts and the ``MetricsAccess`` service Protocol.

These carry the *telemetry* of a running Host — CPU, memory, disk, and per-container
usage — read live over SSH for ``billet host specs``. They are deliberately separate from
:class:`~billet.contracts.host.HostSpec` (operator intent) and
:class:`~billet.contracts.host.HostStatus` (control-plane power state): usage changes on a
different cadence and comes from the data plane, not the backend API.

Host-level numbers are normalized to bytes so the client can render and compare them.
Container-level usage is kept as the verbatim ``docker stats`` strings (``0.15%``,
``1.2GiB / 15.6GiB``): billet reports them, it does not compute on them.
"""

from dataclasses import dataclass
from typing import Protocol

from billet.contracts.workspace import RemoteHost


@dataclass(frozen=True, slots=True)
class CpuMetrics:
    """Core count and load averages from ``nproc`` + ``/proc/loadavg``."""

    cores: int
    load_1m: float
    load_5m: float
    load_15m: float


@dataclass(frozen=True, slots=True)
class MemoryMetrics:
    """Host memory from ``/proc/meminfo`` (``MemAvailable`` is the kernel's own estimate)."""

    total_bytes: int
    available_bytes: int

    @property
    def used_bytes(self) -> int:
        """Bytes not available for new workloads (total minus ``MemAvailable``)."""
        return self.total_bytes - self.available_bytes

    @property
    def used_percent(self) -> float:
        """``used_bytes`` as a percentage of the total (0.0 when total is unknown)."""
        if self.total_bytes == 0:
            return 0.0
        return 100.0 * self.used_bytes / self.total_bytes


@dataclass(frozen=True, slots=True)
class DiskMetrics:
    """One filesystem's usage from ``df -PB1``, deduplicated by device."""

    mount: str
    size_bytes: int
    used_bytes: int
    available_bytes: int

    @property
    def used_percent(self) -> float:
        """``used_bytes`` as a percentage of the size (0.0 when size is unknown)."""
        if self.size_bytes == 0:
            return 0.0
        return 100.0 * self.used_bytes / self.size_bytes


@dataclass(frozen=True, slots=True)
class ContainerMetrics:
    """One running container's usage, verbatim from ``docker stats`` / ``docker ps``."""

    name: str
    status: str
    cpu_percent: str
    mem_usage: str
    mem_percent: str


@dataclass(frozen=True, slots=True)
class HostMetrics:
    """The full live-usage report for one Host — the payload of ``billet host specs``."""

    cpu: CpuMetrics
    memory: MemoryMetrics
    disks: tuple[DiskMetrics, ...]
    containers: tuple[ContainerMetrics, ...]


class MetricsAccess(Protocol):
    """Reads live CPU / memory / disk / container usage from a running Host."""

    def read(self, remote: RemoteHost) -> HostMetrics:
        """Probe ``remote`` over SSH and return its parsed usage report."""
        ...
