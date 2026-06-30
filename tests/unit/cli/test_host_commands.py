"""Tests for the ``billet host`` CLI — routing, dry-run, and the billable confirm gate.

The provider factory is monkeypatched to a fake, so these exercise the full command path
(config parse -> plan -> gate -> apply) without ever invoking ``az`` or ``ssh``.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from billet.cli import host_commands
from billet.cli.app import app
from billet.contracts import HostPowerState, HostProvider, HostStatus
from tests.unit._fakes import FakeHostProvider

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


def test_missing_config_exits_cleanly(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    _install(monkeypatch, FakeHostProvider())
    result = runner.invoke(app, ["host", "up", "--config", str(tmp_path / "absent.toml")])
    assert result.exit_code == 1
    assert result.exception is None or isinstance(result.exception, SystemExit)
