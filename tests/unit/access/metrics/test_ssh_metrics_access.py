"""Tests for SshMetricsAccess — probe argv/script shape and section parsing.

The process runner is mocked (never a real ``ssh``); the probe output is scripted from
fixture strings, exercising the parsers over realistic ``nproc`` / ``/proc`` / ``df`` /
``docker`` output.
"""

import pytest

from billet.access.metrics.ssh_metrics_access import SshMetricsAccess
from billet.shared.errors import HostOperationError
from tests.unit._fakes import FakeProcessRunner, completed, make_remote_host

REMOTE = make_remote_host()

_PROBE_OUTPUT = """\
===cpu===
4
0.42 0.31 0.20 1/234 5678
===mem===
MemTotal:       16393216 kB
MemAvailable:   12582912 kB
===disk===
Filesystem     1-blocks       Used  Available Capacity Mounted on
/dev/root    67371577344 49392123904 17962430464  74% /
/dev/root    67371577344 49392123904 17962430464  74% /
===containers===
gswa-backend|Up 3 hours
billet|Up 2 days
===stats===
gswa-backend|0.15%|1.2GiB / 15.6GiB|7.7%
billet|0.02%|350MiB / 15.6GiB|2.2%
"""


def _access(stdout: str) -> tuple[SshMetricsAccess, FakeProcessRunner]:
    runner = FakeProcessRunner(lambda argv: completed(stdout=stdout))
    return SshMetricsAccess(runner), runner


def test_read_probes_over_batch_mode_ssh() -> None:
    access, runner = _access(_PROBE_OUTPUT)
    access.read(REMOTE)
    assert len(runner.calls) == 1
    command = runner.commands()[0]
    assert command.startswith("ssh ")
    assert "BatchMode=yes" in command
    assert "azureuser@20.0.0.5" in command
    assert command.endswith("bash -se")
    script = runner.inputs[0]
    assert script is not None
    for probe in ("nproc", "/proc/loadavg", "/proc/meminfo", "df -PB1", "docker stats"):
        assert probe in script


def test_read_parses_cpu_memory_and_disk() -> None:
    access, _ = _access(_PROBE_OUTPUT)
    metrics = access.read(REMOTE)
    assert metrics.cpu.cores == 4
    assert (metrics.cpu.load_1m, metrics.cpu.load_5m, metrics.cpu.load_15m) == (0.42, 0.31, 0.20)
    assert metrics.memory.total_bytes == 16393216 * 1024
    assert metrics.memory.available_bytes == 12582912 * 1024
    assert metrics.memory.used_bytes == (16393216 - 12582912) * 1024
    assert 0.0 < metrics.memory.used_percent < 100.0
    assert len(metrics.disks) == 1  # / and /var/lib/docker on one device -> deduplicated
    disk = metrics.disks[0]
    assert disk.mount == "/"
    assert disk.size_bytes == 67371577344
    assert disk.used_bytes == 49392123904
    assert disk.available_bytes == 17962430464
    assert disk.used_percent == pytest.approx(73.3, abs=0.1)


def test_read_joins_container_stats_with_ps_status() -> None:
    access, _ = _access(_PROBE_OUTPUT)
    metrics = access.read(REMOTE)
    assert [c.name for c in metrics.containers] == ["gswa-backend", "billet"]
    backend = metrics.containers[0]
    assert backend.status == "Up 3 hours"
    assert backend.cpu_percent == "0.15%"
    assert backend.mem_usage == "1.2GiB / 15.6GiB"
    assert backend.mem_percent == "7.7%"


def test_read_keeps_distinct_disk_devices() -> None:
    output = _PROBE_OUTPUT.replace(
        "/dev/root    67371577344 49392123904 17962430464  74% /\n"
        "/dev/root    67371577344 49392123904 17962430464  74% /",
        "/dev/root    67371577344 49392123904 17962430464  74% /\n"
        "/dev/sdb1    137438953472 68719476736 68719476736  50% /var/lib/docker",
    )
    access, _ = _access(output)
    metrics = access.read(REMOTE)
    assert [d.mount for d in metrics.disks] == ["/", "/var/lib/docker"]


def test_read_without_docker_reports_no_containers() -> None:
    output = _PROBE_OUTPUT.partition("===containers===")[0] + "===containers===\n===stats===\n"
    access, _ = _access(output)
    metrics = access.read(REMOTE)
    assert metrics.containers == ()
    assert metrics.cpu.cores == 4  # system sections still parse


def test_read_stats_without_ps_row_defaults_status() -> None:
    output = _PROBE_OUTPUT.replace("gswa-backend|Up 3 hours\n", "")
    access, _ = _access(output)
    metrics = access.read(REMOTE)
    assert metrics.containers[0].status == "running"


def test_read_malformed_cpu_section_raises() -> None:
    access, _ = _access(_PROBE_OUTPUT.replace("4\n0.42 0.31 0.20 1/234 5678", "not-a-number"))
    with pytest.raises(HostOperationError, match="'cpu' probe output"):
        access.read(REMOTE)


def test_read_missing_meminfo_field_raises() -> None:
    access, _ = _access(_PROBE_OUTPUT.replace("MemAvailable:   12582912 kB\n", ""))
    with pytest.raises(HostOperationError, match="'mem' probe output"):
        access.read(REMOTE)


def test_read_empty_disk_section_raises() -> None:
    output = (
        "===cpu===\n4\n0.42 0.31 0.20 1/234 5678\n"
        "===mem===\nMemTotal: 1024 kB\nMemAvailable: 512 kB\n"
        "===disk===\nFilesystem     1-blocks       Used  Available Capacity Mounted on\n"
        "===containers===\n===stats===\n"
    )
    access, _ = _access(output)
    with pytest.raises(HostOperationError, match="'disk' probe output"):
        access.read(REMOTE)
