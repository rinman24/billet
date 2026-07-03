"""SshMetricsAccess — read live CPU / memory / disk / container usage over SSH.

Implements ``MetricsAccess``. One agent-less, batch-mode SSH round trip runs a sectioned
probe script (the ``bash -se`` / ``batch_mode=True`` pattern shared with
``ComposeContainerAccess``); the sections are then parsed by pure helpers below.

Probe choices:

- ``/proc/meminfo``'s ``MemAvailable`` (not ``free``'s "free"): the kernel's own estimate
  of what a new workload can claim, counting reclaimable page cache.
- ``df -PB1`` in bytes over ``/`` *and* ``/var/lib/docker``: image layers, build cache, and
  container overlays land under the Docker data root, which may or may not be its own
  filesystem — duplicates are removed by device.
- ``docker stats --no-stream`` + ``docker ps``, joined by container name; both sections are
  best-effort (``|| true``) so a Docker-less host still reports its system numbers. Fields
  use ``|`` as the separator — Docker names and stat values cannot contain it.
"""

from billet.contracts import (
    ContainerMetrics,
    CpuMetrics,
    DiskMetrics,
    HostMetrics,
    MemoryMetrics,
    RemoteHost,
)
from billet.infrastructure import ssh
from billet.infrastructure.process import ProcessRunner
from billet.shared.errors import HostOperationError

_KIB = 1024

_PROBE_SCRIPT = """set -eu
echo ===cpu===
nproc
cat /proc/loadavg
echo ===mem===
grep -E '^(MemTotal|MemAvailable):' /proc/meminfo
echo ===disk===
df -PB1 / /var/lib/docker 2>/dev/null || df -PB1 /
echo ===containers===
docker ps --format '{{.Names}}|{{.Status}}' 2>/dev/null || true
echo ===stats===
docker stats --no-stream --format '{{.Name}}|{{.CPUPerc}}|{{.MemUsage}}|{{.MemPerc}}' \
  2>/dev/null || true
"""


class SshMetricsAccess:
    """A ``MetricsAccess`` over one sectioned probe script run on the Host via SSH."""

    def __init__(self, runner: ProcessRunner) -> None:
        self._runner = runner

    def read(self, remote: RemoteHost) -> HostMetrics:
        """Run the probe script on ``remote`` and parse its output into metrics."""
        argv = ssh.ssh_argv(remote.admin_user, remote.ip, "bash -se", batch_mode=True)
        result = self._runner.run(argv, input_text=_PROBE_SCRIPT)
        return _metrics_from_output(result.stdout)


# --- pure parsing over the sectioned probe output ------------------------------------


def _metrics_from_output(text: str) -> HostMetrics:
    sections = _split_sections(text)
    return HostMetrics(
        cpu=_parse_cpu(sections.get("cpu", [])),
        memory=_parse_memory(sections.get("mem", [])),
        disks=_parse_disks(sections.get("disk", [])),
        containers=_parse_containers(sections.get("containers", []), sections.get("stats", [])),
    )


def _split_sections(text: str) -> dict[str, list[str]]:
    """Group the probe's non-empty output lines under their ``===name===`` markers."""
    sections: dict[str, list[str]] = {}
    current: list[str] | None = None
    for line in text.splitlines():
        stripped = line.strip()
        if not stripped:
            continue
        if stripped.startswith("===") and stripped.endswith("==="):
            current = sections.setdefault(stripped.strip("="), [])
        elif current is not None:
            current.append(stripped)
    return sections


def _parse_cpu(lines: list[str]) -> CpuMetrics:
    """Parse the ``nproc`` line then the ``/proc/loadavg`` line."""
    expected_lines = 2
    expected_loads = 3
    if len(lines) < expected_lines:
        raise HostOperationError(_malformed("cpu", lines))
    loads = lines[1].split()
    try:
        cores = int(lines[0])
        load_1m, load_5m, load_15m = (float(value) for value in loads[:expected_loads])
    except ValueError as exc:
        raise HostOperationError(_malformed("cpu", lines)) from exc
    return CpuMetrics(cores=cores, load_1m=load_1m, load_5m=load_5m, load_15m=load_15m)


def _parse_memory(lines: list[str]) -> MemoryMetrics:
    """Parse ``MemTotal:``/``MemAvailable:`` meminfo lines (values are in KiB)."""
    values: dict[str, int] = {}
    for line in lines:
        key, _, rest = line.partition(":")
        fields = rest.split()
        if not fields:
            continue
        try:
            values[key] = int(fields[0]) * _KIB
        except ValueError as exc:
            raise HostOperationError(_malformed("mem", lines)) from exc
    if "MemTotal" not in values or "MemAvailable" not in values:
        raise HostOperationError(_malformed("mem", lines))
    return MemoryMetrics(total_bytes=values["MemTotal"], available_bytes=values["MemAvailable"])


def _parse_disks(lines: list[str]) -> tuple[DiskMetrics, ...]:
    """Parse ``df -PB1`` body lines, keeping the first entry per device.

    ``/`` and ``/var/lib/docker`` are probed together and are often the same filesystem,
    in which case ``df`` reports the device once per argument.
    """
    columns = 6
    disks: list[DiskMetrics] = []
    seen_devices: set[str] = set()
    for line in lines:
        fields = line.split(None, columns - 1)
        if len(fields) < columns or fields[0] == "Filesystem":
            continue
        device, size, used, available, _, mount = fields
        if device in seen_devices:
            continue
        seen_devices.add(device)
        try:
            disks.append(
                DiskMetrics(
                    mount=mount,
                    size_bytes=int(size),
                    used_bytes=int(used),
                    available_bytes=int(available),
                )
            )
        except ValueError as exc:
            raise HostOperationError(_malformed("disk", lines)) from exc
    if not disks:
        raise HostOperationError(_malformed("disk", lines))
    return tuple(disks)


def _parse_containers(ps_lines: list[str], stats_lines: list[str]) -> tuple[ContainerMetrics, ...]:
    """Join ``docker stats`` usage with ``docker ps`` status by container name.

    Both sections are best-effort on the host, so an absent Docker (or a container that
    raced away between the two commands) degrades to fewer rows, never an error.
    """
    stats_fields = 4
    statuses: dict[str, str] = {}
    for line in ps_lines:
        name, _, status = line.partition("|")
        statuses[name] = status
    containers: list[ContainerMetrics] = []
    for line in stats_lines:
        fields = line.split("|")
        if len(fields) != stats_fields:
            continue
        name, cpu_percent, mem_usage, mem_percent = fields
        containers.append(
            ContainerMetrics(
                name=name,
                status=statuses.get(name, "running"),
                cpu_percent=cpu_percent,
                mem_usage=mem_usage,
                mem_percent=mem_percent,
            )
        )
    return tuple(containers)


def _malformed(section: str, lines: list[str]) -> str:
    body = "\n".join(lines) or "<empty>"
    return f"could not parse the host's '{section}' probe output:\n{body}"
