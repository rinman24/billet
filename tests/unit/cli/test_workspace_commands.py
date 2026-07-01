"""Tests for the top-level Workspace CLI — routing, dry-run, host orchestration, connect.

The provider and workspace-manager factories are monkeypatched to fakes, so these exercise
the full command path (config parse -> host plan/gate -> workspace plan/apply) without ever
invoking ``az`` / ``ssh`` / ``os.execvp``.
"""

from pathlib import Path

import pytest
from typer.testing import CliRunner

from billet.cli import workspace_commands as wc
from billet.cli.app import app
from billet.contracts import HostPowerState, HostStatus
from billet.workspace.manager.workspace_manager import WorkspaceManager
from tests.unit._fakes import (
    FakeContainerAccess,
    FakeHostProvider,
    FakeSourceAccess,
    FakeSshConfigAccess,
)

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

[workspaces.gswa-backend]
host = "devbox"
repo_url = "git@github.com:genshift/gswa-backend.git"
repo_dir = "gswa-backend"
container_ssh_port = 2222
host_alias = "gswa-devbox"
container_alias = "gswa-container"
"""


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG)
    return path


def _install(
    monkeypatch: pytest.MonkeyPatch,
    *,
    provider: FakeHostProvider | None = None,
    source: FakeSourceAccess | None = None,
    container: FakeContainerAccess | None = None,
    ssh_config: FakeSshConfigAccess | None = None,
) -> tuple[FakeHostProvider, FakeSourceAccess, FakeContainerAccess, FakeSshConfigAccess]:
    prov = provider or FakeHostProvider(
        HostStatus(HostPowerState.RUNNING, "20.0.0.5", "VM running")
    )
    src = source or FakeSourceAccess()
    cont = container or FakeContainerAccess()
    cfg = ssh_config or FakeSshConfigAccess()
    manager = WorkspaceManager(src, cont, cfg)

    def _provider_factory(_subscription_id: str) -> FakeHostProvider:
        return prov

    def _manager_factory() -> WorkspaceManager:
        return manager

    monkeypatch.setattr(wc, "provider_factory", _provider_factory)
    monkeypatch.setattr(wc, "workspace_manager_factory", _manager_factory)
    return prov, src, cont, cfg


# --- add ---------------------------------------------------------------------------


def test_add_validates_and_prints_block(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    _install(monkeypatch)
    result = runner.invoke(app, ["add", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "[workspaces.gswa-backend]" in result.output
    assert "is valid" in result.output


def test_add_unknown_workspace_exits_cleanly(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _install(monkeypatch)
    result = runner.invoke(app, ["add", "ghost", "--config", str(config_file)])
    assert result.exit_code == 1


# --- ls ----------------------------------------------------------------------------


def test_ls_reports_running_state(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    _install(monkeypatch, container=FakeContainerAccess(running=True))
    result = runner.invoke(app, ["ls", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "gswa-backend" in result.output
    assert "running" in result.output


# --- start -------------------------------------------------------------------------


def test_start_dry_run_renders_both_plans_without_applying(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    prov, src, cont, _ = _install(
        monkeypatch, provider=FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, ""))
    )
    result = runner.invoke(
        app, ["start", "gswa-backend", "--config", str(config_file), "--dry-run"]
    )
    assert result.exit_code == 0
    assert "plan for host" in result.output
    assert "plan for workspace" in result.output
    assert "dry-run" in result.output
    assert src.calls == []
    assert cont.calls == []
    assert "create" not in prov.calls


def test_start_billable_decline_aborts_before_workspace(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _, src, cont, _ = _install(
        monkeypatch, provider=FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, ""))
    )
    result = runner.invoke(
        app, ["start", "gswa-backend", "--config", str(config_file)], input="n\n"
    )
    assert result.exit_code == 1
    assert "aborted" in result.output.lower()
    assert src.calls == []
    assert cont.calls == []


def test_start_running_host_applies_host_then_workspace(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    prov, src, cont, _ = _install(monkeypatch)  # default provider is RUNNING with an IP
    result = runner.invoke(app, ["start", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "ensure_tags" in prov.calls  # adoption of the live VM
    assert src.calls == [("gswa-backend", "20.0.0.5")]
    assert cont.calls == ["read_facts", "compose_up", "run_post_create"]
    assert "is up" in result.output


def test_start_with_verify_runs_verify(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    _, _, cont, _ = _install(monkeypatch)
    result = runner.invoke(app, ["start", "gswa-backend", "--config", str(config_file), "--verify"])
    assert result.exit_code == 0
    assert "verify" in cont.calls


# --- stop --------------------------------------------------------------------------


def test_stop_applies_compose_stop(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    _, _, cont, _ = _install(monkeypatch)
    result = runner.invoke(app, ["stop", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    assert cont.calls == ["read_facts", "compose_stop"]
    assert "stopped" in result.output


def test_stop_dry_run_does_not_apply(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    _, _, cont, _ = _install(monkeypatch)
    result = runner.invoke(app, ["stop", "gswa-backend", "--config", str(config_file), "--dry-run"])
    assert result.exit_code == 0
    assert cont.calls == []


# --- connect -----------------------------------------------------------------------


def test_connect_execs_tmux_argv(monkeypatch: pytest.MonkeyPatch, config_file: Path) -> None:
    _install(monkeypatch)
    captured: dict[str, list[str]] = {}

    def _spy_execvp(argv: list[str]) -> None:
        captured["argv"] = argv

    monkeypatch.setattr(wc, "_execvp", _spy_execvp)
    result = runner.invoke(app, ["connect", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    argv = captured["argv"]
    assert argv[0] == "ssh"
    assert "-t" in argv
    assert "gswa-container" in argv
    assert argv[-1] == "cd /app && exec tmux new-session -A -s main bash -l"


# --- ssh-config --------------------------------------------------------------------


def test_ssh_config_writes_conf_and_include(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _, _, _, cfg = _install(monkeypatch)
    result = runner.invoke(app, ["ssh-config", "--config", str(config_file)])
    assert result.exit_code == 0
    assert cfg.written is not None
    assert "Host gswa-container" in cfg.written
    assert "ProxyJump gswa-devbox" in cfg.written
    assert cfg.include_calls == 1
    assert "wrote" in result.output


def test_ssh_config_dry_run_prints_without_writing(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _, _, _, cfg = _install(monkeypatch)
    result = runner.invoke(app, ["ssh-config", "--config", str(config_file), "--dry-run"])
    assert result.exit_code == 0
    assert "Host gswa-container" in result.output
    assert "dry-run" in result.output
    assert cfg.written is None


# --- rm ----------------------------------------------------------------------------


def test_rm_prints_deregistration_guidance(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _install(monkeypatch)
    result = runner.invoke(app, ["rm", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "stateless" in result.output
    assert "billet stop gswa-backend" in result.output
