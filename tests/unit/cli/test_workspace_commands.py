"""Tests for the top-level Workspace CLI — routing, dry-run, host orchestration, connect.

The provider and workspace-manager factories are monkeypatched to fakes, so these exercise
the full command path (config parse -> host plan/gate -> workspace plan/apply) without ever
invoking ``az`` / ``ssh`` / ``os.execvp``.
"""

from collections.abc import Sequence
import json
from pathlib import Path

import pytest
from typer.testing import CliRunner

from billet.cli import _ui, workspace_commands as wc
from billet.cli.app import app
from billet.contracts import HostPowerState, HostStatus
from billet.shared.errors import HostOperationError
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

    def _manager_factory(on_line: object = None) -> WorkspaceManager:
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
    assert "workspace gswa-backend · valid" in result.output
    assert "billet start gswa-backend" in result.output  # the next-step hint


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


def test_ls_probes_each_host_exactly_once(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    # The header power/size/ip probe: exactly one provider.status per host in _CONFIG.
    prov, _, _, _ = _install(monkeypatch)
    result = runner.invoke(app, ["ls", "--config", str(config_file)])
    assert result.exit_code == 0
    assert prov.calls.count("status") == 1  # one [hosts.*] table


def test_ls_probes_every_host_including_non_managing(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The header probe is orthogonal to ADR-0004: even a non-managing host is probed once.
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG + _ADOPTED_HOST)
    prov, _, _, _ = _install(monkeypatch, container=FakeContainerAccess(running=True))
    result = runner.invoke(app, ["ls", "--config", str(path)])
    assert result.exit_code == 0
    assert prov.calls.count("status") == 2  # devbox + fleet


def test_ls_notexist_host_does_not_crash(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _install(monkeypatch, provider=FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, "")))
    result = runner.invoke(app, ["ls", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "gswa-backend" in result.output


def _capture_ls_groups(monkeypatch: pytest.MonkeyPatch) -> list[_ui.LsHostGroup]:
    """Intercept the groups ``ls`` builds by stubbing the public render step."""
    captured: list[_ui.LsHostGroup] = []

    def _render(groups: Sequence[_ui.LsHostGroup], console: object = None) -> None:
        captured.extend(groups)

    monkeypatch.setattr(_ui, "render_ls", _render)
    return captured


def test_ls_shows_live_vm_size_over_config(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    # A live probe wins over the configured size; the header power/ip come from the probe.
    captured = _capture_ls_groups(monkeypatch)
    _install(
        monkeypatch,
        provider=FakeHostProvider(
            HostStatus(HostPowerState.RUNNING, "20.0.0.5", "up", vm_size="Standard_D8s_v5")
        ),
    )
    result = runner.invoke(app, ["ls", "--config", str(config_file)])
    assert result.exit_code == 0
    devbox = next(group for group in captured if group.key == "devbox")
    assert devbox.vm_size == "Standard_D8s_v5"  # live, not the configured Standard_D4s_v4
    assert devbox.power_state is HostPowerState.RUNNING
    assert devbox.public_ip == "20.0.0.5"


def test_ls_falls_back_to_config_vm_size_when_probe_is_blank(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    captured = _capture_ls_groups(monkeypatch)
    _install(
        monkeypatch,
        provider=FakeHostProvider(HostStatus(HostPowerState.DEALLOCATED, None, "", vm_size=None)),
    )
    result = runner.invoke(app, ["ls", "--config", str(config_file)])
    assert result.exit_code == 0
    devbox = next(group for group in captured if group.key == "devbox")
    assert devbox.vm_size == "Standard_D4s_v4"  # the configured size, since the probe was blank
    assert devbox.public_ip is None


def test_ls_reports_unreachable_host_without_hanging_or_failing(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    # A deallocated host is a normal state (`billet host stop`): ls stays a
    # successful query and names the recovery command (ADR-0004 §2).
    class UnreachableContainerAccess(FakeContainerAccess):
        def read_facts(self, spec, remote):  # type: ignore[no-untyped-def]
            raise HostOperationError("could not reach 20.0.0.5 over SSH")

    _install(monkeypatch, container=UnreachableContainerAccess())
    result = runner.invoke(app, ["ls", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "unreachable" in result.output
    assert "billet host up --host devbox" in result.output


# An adopted host billet never provisions: no vm_image / vm_size / … keys at all.
_ADOPTED_HOST = """
[hosts.fleet]
resource_group = "GSWA-FLEET-HOST-RG"
vm_name = "gswa-fleet-host"
location = "westus3"
admin_user = "azureuser"
manages_workspaces = false
"""


def test_ls_is_a_pure_query_over_an_adopted_host_without_provisioning_keys(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    # The PR #33 regression: ls parses every [hosts.*] table for its racks view, so an
    # adopted host with no provisioning keys must not fail the query.
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG + _ADOPTED_HOST)
    _install(monkeypatch, container=FakeContainerAccess(running=True))
    result = runner.invoke(app, ["ls", "--config", str(path)])
    assert result.exit_code == 0  # parsing [hosts.fleet] must not hard-fail the query
    assert "gswa-backend" in result.output
    json_result = runner.invoke(app, ["ls", "--config", str(path), "--json"])
    assert json_result.exit_code == 0
    assert json.loads(json_result.stdout)  # still one valid record per workspace


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
    assert "plan · host" in result.output
    assert "plan · workspace" in result.output
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


def test_start_runs_personal_bootstrap_from_global_config(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    text = _CONFIG.replace(
        'default_host = "devbox"',
        'default_host = "devbox"\npersonal_bootstrap_cmd = "bash ~/dotfiles/install.sh"',
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    _, _, cont, _ = _install(monkeypatch)
    result = runner.invoke(app, ["start", "gswa-backend", "--config", str(path)])
    assert result.exit_code == 0
    assert cont.calls == ["read_facts", "compose_up", "run_post_create", "run_personal_bootstrap"]
    assert cont.personal_bootstrap_cmds == ["bash ~/dotfiles/install.sh"]


class _FakeTokenAccess:
    """Records the command strings passed to resolve and returns a configured token."""

    def __init__(self, token: str | None) -> None:
        self._token = token
        self.commands: list[str] = []

    def resolve(self, command: str) -> str | None:
        self.commands.append(command)
        return self._token if command else None


def _config_with_token_cmd(tmp_path: Path) -> Path:
    text = _CONFIG.replace(
        'default_host = "devbox"',
        'default_host = "devbox"\nclaude_token_cmd = "printf tok-abc"',
    )
    path = tmp_path / "config.toml"
    path.write_text(text)
    return path


def test_start_resolves_and_threads_the_claude_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _config_with_token_cmd(tmp_path)
    _, _, cont, _ = _install(monkeypatch)
    fake = _FakeTokenAccess("tok-abc")
    monkeypatch.setattr(wc, "claude_token_access_factory", lambda: fake)
    result = runner.invoke(app, ["start", "gswa-backend", "--config", str(path)])
    assert result.exit_code == 0
    assert fake.commands == ["printf tok-abc"]  # the operator's local fetch command, verbatim
    assert cont.claude_oauth_tokens == ["tok-abc"]  # threaded into compose_up
    assert "tok-abc" not in result.output  # never echoed to the operator


def test_start_threads_no_token_when_cmd_absent(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    # Default config has no claude_token_cmd: the real resolver returns None without shelling out.
    _, _, cont, _ = _install(monkeypatch)
    result = runner.invoke(app, ["start", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    assert cont.claude_oauth_tokens == [None]


def test_start_dry_run_does_not_fetch_the_token(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    path = _config_with_token_cmd(tmp_path)
    _install(monkeypatch, provider=FakeHostProvider(HostStatus(HostPowerState.NOTEXIST, None, "")))
    fake = _FakeTokenAccess("tok-abc")
    monkeypatch.setattr(wc, "claude_token_access_factory", lambda: fake)
    result = runner.invoke(app, ["start", "gswa-backend", "--config", str(path), "--dry-run"])
    assert result.exit_code == 0
    assert fake.commands == []  # no secret fetch on the planning path


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
    assert argv[-1] == (
        "cd /app && exec env LC_ALL=C.UTF-8 LANG=C.UTF-8 TERM=xterm-256color tmux "
        "set -g status-left ' gswa-backend ' \\; set -g status-left-length 14 \\; "
        "new-session -A -s main bash -l"
    )


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


# --- multi-workspace (slice 6) -----------------------------------------------------

_SECOND_WS = """
[workspaces.other-repo]
host = "devbox"
repo_url = "git@github.com:my-org/other-repo.git"
repo_dir = "other-repo"
container_ssh_port = 2223
host_alias = "gswa-devbox"
container_alias = "other-container"
"""


@pytest.fixture
def two_ws_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG + _SECOND_WS)
    return path


def test_ssh_config_renders_both_workspaces_one_host(
    monkeypatch: pytest.MonkeyPatch, two_ws_config: Path
) -> None:
    _, _, _, cfg = _install(monkeypatch)
    result = runner.invoke(app, ["ssh-config", "--config", str(two_ws_config)])
    assert result.exit_code == 0
    conf = cfg.written
    assert conf is not None
    # One shared host entry, two distinct container entries + ports + HostKeyAliases.
    assert conf.count("Host gswa-devbox\n") == 1
    assert "Host gswa-container" in conf
    assert "Host other-container" in conf
    assert "Port 2222" in conf
    assert "Port 2223" in conf
    assert "HostKeyAlias gswa-container" in conf
    assert "HostKeyAlias other-container" in conf


def test_ls_lists_both_workspaces(monkeypatch: pytest.MonkeyPatch, two_ws_config: Path) -> None:
    _install(monkeypatch)
    result = runner.invoke(app, ["ls", "--config", str(two_ws_config)])
    assert result.exit_code == 0
    assert "gswa-backend" in result.output
    assert "other-repo" in result.output


def test_add_detects_a_port_collision_on_the_same_host(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    colliding = _SECOND_WS.replace("container_ssh_port = 2223", "container_ssh_port = 2222")
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG + colliding)
    _install(monkeypatch)
    result = runner.invoke(app, ["add", "other-repo", "--config", str(path)])
    assert result.exit_code == 1
    assert "collision" in result.output.lower()


# --- fleet host / manages_workspaces enforcement (slice 7, ADR-0004) ---------------

# A non-managing Host (the fleet-host) plus a Workspace an operator wrongly placed on it.
_FLEET_HOST_AND_WS = """
[hosts.fleet]
resource_group = "GSWA-FLEET-HOST-RG"
vm_name = "gswa-fleet-host"
location = "westus3"
admin_user = "azureuser"
vm_image = "img"
vm_size = "Standard_D4s_v5"
public_ip_sku = "Standard"
os_disk_gb = 64
storage_sku = "Premium_LRS"
manages_workspaces = false

[workspaces.on-fleet]
host = "fleet"
repo_url = "git@github.com:my-org/on-fleet.git"
repo_dir = "on-fleet"
container_ssh_port = 2224
host_alias = "gswa-fleet-host"
container_alias = "on-fleet-container"
"""


@pytest.fixture
def fleet_config(tmp_path: Path) -> Path:
    path = tmp_path / "config.toml"
    path.write_text(_CONFIG + _FLEET_HOST_AND_WS)
    return path


@pytest.mark.parametrize("verb", ["add", "start", "stop", "connect"])
def test_command_verbs_refuse_a_non_managing_host(
    monkeypatch: pytest.MonkeyPatch, fleet_config: Path, verb: str
) -> None:
    _install(monkeypatch)
    result = runner.invoke(app, [verb, "on-fleet", "--config", str(fleet_config)])
    assert result.exit_code == 1
    assert "manages_workspaces" in result.output
    assert "billet host" in result.output  # points at the lifecycle escape hatch


def test_ssh_config_refuses_when_any_workspace_is_on_a_non_managing_host(
    monkeypatch: pytest.MonkeyPatch, fleet_config: Path
) -> None:
    _, _, _, cfg = _install(monkeypatch)
    result = runner.invoke(app, ["ssh-config", "--config", str(fleet_config)])
    assert result.exit_code == 1
    assert "manages_workspaces" in result.output
    assert cfg.written is None  # a command fails closed — nothing rendered


def test_ls_annotates_a_non_managing_host_but_still_lists_the_rest(
    monkeypatch: pytest.MonkeyPatch, fleet_config: Path
) -> None:
    # ls is a query (ADR-0004 §2): it surfaces the misconfig inline, never raises.
    _install(monkeypatch, container=FakeContainerAccess(running=True))
    result = runner.invoke(app, ["ls", "--config", str(fleet_config)])
    assert result.exit_code == 0
    rows = [ln for ln in result.output.splitlines() if ln.strip()]
    backend = next(ln for ln in rows if "gswa-backend" in ln)
    assert "running" in backend  # the managing-host workspace still probed
    on_fleet = next(ln for ln in rows if "on-fleet" in ln)
    assert "invalid" in on_fleet  # the fleet-host workspace flagged, not probed


def test_ls_json_emits_machine_readable_records(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _install(monkeypatch, container=FakeContainerAccess(running=True))
    result = runner.invoke(app, ["ls", "--config", str(config_file), "--json"])
    assert result.exit_code == 0
    records = json.loads(result.stdout)
    assert records == [
        {
            "host": "devbox",
            "key": "gswa-backend",
            "state": "running",
            "alias": "gswa-container",
            "port": 2222,
        }
    ]


def test_ls_json_output_has_no_status_chrome(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    # --- json is machine-readable: the phase-status line must never leak into stdout, so the
    # whole output parses as JSON with no stray spinner/ansi characters.
    _install(monkeypatch, container=FakeContainerAccess(running=True))
    result = runner.invoke(app, ["ls", "--config", str(config_file), "--json"])
    assert result.exit_code == 0
    json.loads(result.stdout)  # parses clean — no stray chrome around the records


def test_connect_prints_status_before_exec(
    monkeypatch: pytest.MonkeyPatch, config_file: Path
) -> None:
    _install(monkeypatch)

    def _no_exec(argv: list[str]) -> None:
        return None

    monkeypatch.setattr(wc, "_execvp", _no_exec)
    result = runner.invoke(app, ["connect", "gswa-backend", "--config", str(config_file)])
    assert result.exit_code == 0
    assert "connecting to gswa-backend" in result.output
    assert "tmux main" in result.output
