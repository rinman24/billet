"""Tests for the ``billet host`` CLI — routing, dry-run, and the billable confirm gate.

The provider factory is monkeypatched to a fake, so these exercise the full command path
(config parse -> plan -> gate -> apply) without ever invoking ``az`` or ``ssh``.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from billet.cli import host_commands
from billet.cli.app import app
from billet.contracts import (
    ContainerMetrics,
    HostPowerState,
    HostProvider,
    HostSpec,
    HostStatus,
    MetricsAccess,
)
from billet.shared.errors import HostOperationError
from tests.unit._fakes import FakeHostProvider, FakeMetricsAccess, make_host_metrics

runner = CliRunner()

_CONFIG = """
[billet]
subscription_id = "sub-123"
default_host = "devbox"

[hosts.devbox]
resource_group = "gswa-devbox-rg"
vm_name = "gswa-devbox"
location = "westus3"
admin_user = "azureuser"
vm_image = "img"
vm_size = "Standard_D4s_v4"
public_ip_sku = "Standard"
os_disk_gb = 64
storage_sku = "Premium_LRS"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG)
    return path


def _install(monkeypatch: pytest.MonkeyPatch, provider: FakeHostProvider) -> None:
    def factory(subscription_id: str) -> HostProvider:
        return provider

    monkeypatch.setattr(host_commands, "provider_factory", factory)


def test_up_dry_run_renders_plan_without_applying(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "up", "--config", str(config_file), "--dry-run"])
    assert result.exit_code == 0
    assert "dry-run" in result.output
    assert "BILLABLE" in result.output
    assert provider.calls == ["preflight", "status"]
    assert "create" not in provider.calls


def test_up_billable_confirm_declined_aborts(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "up", "--config", str(config_file)], input="n\n")
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert "create" not in provider.calls


def test_up_billable_confirm_accepted_applies(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "up", "--config", str(config_file)], input="y\n")
    assert result.exit_code == 0
    assert "create" in provider.calls
    assert "is up" in result.output


def test_up_yes_skips_confirmation(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "up", "--config", str(config_file), "--yes"])
    assert result.exit_code == 0
    assert "create" in provider.calls


def test_up_resume_applies_without_a_prompt(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.DEALLOCATED, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "up", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "start" in provider.calls
    assert "create" not in provider.calls


def test_up_apply_failure_marks_the_step_failed_before_the_error(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    class ExplodingProvider(FakeHostProvider):
        def start(self, spec: HostSpec) -> None:
            super().start(spec)
            raise HostOperationError("az start failed")

    provider = ExplodingProvider(HostStatus(HostPowerState.DEALLOCATED, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "up", "--config", str(config_file)])
    assert result.exit_code == 1
    lines = result.output.splitlines()
    failed = next(index for index, line in enumerate(lines) if "✗ start VM" in line)
    error = next(index for index, line in enumerate(lines) if "✗ az start failed" in line)
    assert failed < error  # the red ✗ lands before the error report
    assert "wait_until_reachable" not in provider.calls  # later steps never ran


def test_stop_already_deallocated_reports_nothing_to_do(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.DEALLOCATED, None, ""))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "stop", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "nothing to do" in result.output
    assert "deallocate" not in provider.calls


def test_pin_ip_applies_the_pin(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    _install(monkeypatch, provider)
    result = runner.invoke(app, ["host", "pin-ip", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "pin_inbound" in provider.calls


def _install_metrics(monkeypatch: pytest.MonkeyPatch, metrics_access: FakeMetricsAccess) -> None:
    def factory() -> MetricsAccess:
        return metrics_access

    monkeypatch.setattr(host_commands, "metrics_factory", factory)


def test_specs_renders_the_usage_report(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    metrics_access = FakeMetricsAccess()
    _install(monkeypatch, provider)
    _install_metrics(monkeypatch, metrics_access)
    result = runner.invoke(app, ["host", "specs", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "host 'devbox'" in result.output
    assert "4 cores" in result.output
    assert "4.0 GiB used of 16.0 GiB" in result.output
    assert "gswa-backend" in result.output
    assert [r.ip for r in metrics_access.remotes] == ["1.2.3.4"]


def test_specs_renders_usage_bars_normalized_to_the_host(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    container = ContainerMetrics(
        name="devcontainer-billet-1",
        status="Up 52 minutes",
        cpu_percent="50.00%",
        mem_usage="427.2MiB / 15.57GiB",
        mem_percent="2.68%",
    )
    metrics_access = FakeMetricsAccess(make_host_metrics(containers=(container,)))
    _install(monkeypatch, provider)
    _install_metrics(monkeypatch, metrics_access)
    # A wide virtual terminal so Rich does not truncate the container table.
    result = runner.invoke(
        app, ["host", "specs", "--config", str(config_file)], env={"COLUMNS": "120"}
    )
    assert result.exit_code == 0
    assert "█" in result.output  # bars are drawn
    assert "25.0%" in result.output  # host mem: 4 GiB of 16 GiB
    assert "12.5%" in result.output  # container cpu: 50% of one core / 4 cores
    assert "2.7%" in result.output  # container mem, parsed from docker's "2.68%"
    assert "427.2MiB" in result.output


def test_specs_renders_unparseable_docker_percents_verbatim(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    container = ContainerMetrics(
        name="devcontainer-billet-1",
        status="Up 52 minutes",
        cpu_percent="--",
        mem_usage="-- / --",
        mem_percent="--",
    )
    metrics_access = FakeMetricsAccess(make_host_metrics(containers=(container,)))
    _install(monkeypatch, provider)
    _install_metrics(monkeypatch, metrics_access)
    result = runner.invoke(app, ["host", "specs", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "--" in result.output


def test_specs_on_a_deallocated_host_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    provider = FakeHostProvider(HostStatus(HostPowerState.DEALLOCATED, None, "VM deallocated"))
    metrics_access = FakeMetricsAccess()
    _install(monkeypatch, provider)
    _install_metrics(monkeypatch, metrics_access)
    result = runner.invoke(app, ["host", "specs", "--config", str(config_file)])
    assert result.exit_code == 1
    assert "not running" in result.output
    assert metrics_access.remotes == []


def test_missing_config_exits_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, FakeHostProvider())
    result = runner.invoke(app, ["host", "up", "--config", str(tmp_path / "absent.toml")])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
