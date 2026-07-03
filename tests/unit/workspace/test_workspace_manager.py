"""Tests for WorkspaceManager — plan composition, apply dispatch, connect argv, ssh-config."""

import pytest

from billet.contracts import SshConfigBlock, WorkspaceStepKind
from billet.shared.errors import ConfigError
from billet.workspace.manager.workspace_manager import WorkspaceManager
from tests.unit._fakes import (
    FakeContainerAccess,
    FakeSourceAccess,
    FakeSshConfigAccess,
    make_devcontainer_facts,
    make_remote_host,
    make_workspace_spec,
)

SPEC = make_workspace_spec()
REMOTE = make_remote_host()
FACTS = make_devcontainer_facts()


def _manager(
    *,
    container: FakeContainerAccess | None = None,
    source: FakeSourceAccess | None = None,
    ssh_config: FakeSshConfigAccess | None = None,
) -> tuple[WorkspaceManager, FakeSourceAccess, FakeContainerAccess, FakeSshConfigAccess]:
    src = source or FakeSourceAccess()
    cont = container or FakeContainerAccess()
    cfg = ssh_config or FakeSshConfigAccess()
    return WorkspaceManager(src, cont, cfg), src, cont, cfg


# --- register ----------------------------------------------------------------------


def test_register_renders_a_pasteable_block() -> None:
    manager, *_ = _manager()
    block = manager.register(SPEC, existing=[])
    assert block.startswith("[workspaces.gswa-backend]")
    assert 'host = "devbox"' in block
    assert "container_ssh_port = 2222" in block
    assert 'container_alias = "gswa-container"' in block


def test_register_omits_status_color_when_unset() -> None:
    manager, *_ = _manager()
    block = manager.register(SPEC, existing=[])  # SPEC.status_color is None
    assert "status_color" not in block


def test_register_renders_status_color_when_set() -> None:
    manager, *_ = _manager()
    spec = make_workspace_spec(status_color="#C05CE0")
    block = manager.register(spec, existing=[])
    assert 'status_color = "#C05CE0"' in block


def test_register_rejects_a_port_collision() -> None:
    manager, *_ = _manager()
    other = make_workspace_spec(key="other", host="devbox", container_ssh_port=2222)
    with pytest.raises(ConfigError, match="port collision"):
        manager.register(SPEC, existing=[other])


# --- start -------------------------------------------------------------------------


def test_plan_start_orders_steps_and_omits_verify_by_default() -> None:
    manager, *_ = _manager()
    plan = manager.plan_start(SPEC, verify=False)
    assert [s.kind for s in plan.steps] == [
        WorkspaceStepKind.ENSURE_SOURCE,
        WorkspaceStepKind.COMPOSE_UP,
        WorkspaceStepKind.POST_CREATE,
    ]


def test_plan_start_appends_verify_when_requested() -> None:
    manager, *_ = _manager()
    plan = manager.plan_start(SPEC, verify=True)
    assert plan.steps[-1].kind is WorkspaceStepKind.VERIFY


def test_apply_start_clones_reads_facts_then_drives_compose_in_order() -> None:
    manager, source, container, _ = _manager()
    plan = manager.plan_start(SPEC, verify=True)
    facts = manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="")
    assert source.calls == [("gswa-backend", "20.0.0.5")]
    assert container.calls == ["read_facts", "compose_up", "run_post_create", "verify"]
    assert facts.service == "gswa-backend"


def test_apply_start_without_verify_skips_verify() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_start(SPEC, verify=False)
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="")
    assert "verify" not in container.calls


def test_plan_start_slots_personal_bootstrap_between_post_create_and_verify() -> None:
    manager, *_ = _manager()
    plan = manager.plan_start(SPEC, verify=True, personal_bootstrap_cmd="bash install.sh")
    assert [s.kind for s in plan.steps] == [
        WorkspaceStepKind.ENSURE_SOURCE,
        WorkspaceStepKind.COMPOSE_UP,
        WorkspaceStepKind.POST_CREATE,
        WorkspaceStepKind.PERSONAL_BOOTSTRAP,
        WorkspaceStepKind.VERIFY,
    ]


def test_apply_start_runs_personal_bootstrap_after_post_create() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_start(SPEC, verify=False, personal_bootstrap_cmd="bash install.sh")
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="bash install.sh")
    assert container.calls == [
        "read_facts",
        "compose_up",
        "run_post_create",
        "run_personal_bootstrap",
    ]
    assert container.personal_bootstrap_cmds == ["bash install.sh"]


def test_start_skips_personal_bootstrap_when_empty() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_start(SPEC, verify=False)
    assert WorkspaceStepKind.PERSONAL_BOOTSTRAP not in {s.kind for s in plan.steps}
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="")
    assert "run_personal_bootstrap" not in container.calls


# --- stop --------------------------------------------------------------------------


def test_apply_stop_reads_facts_then_stops() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_stop(SPEC)
    manager.apply_stop(plan, SPEC, REMOTE)
    assert container.calls == ["read_facts", "compose_stop"]


# --- connect -----------------------------------------------------------------------


def test_connect_target_builds_tty_tmux_argv_through_the_container_alias() -> None:
    manager, *_ = _manager()
    argv = manager.connect_target(SPEC, FACTS)
    assert argv[0] == "ssh"
    assert "-t" in argv
    assert "gswa-container" in argv  # via the alias (no user@host)
    assert argv[-1] == (
        "cd /app && exec env LC_ALL=C.UTF-8 LANG=C.UTF-8 tmux "
        "set -g status-left ' gswa-backend ' \\; set -g status-left-length 14 \\; "
        "new-session -A -s main bash -l"
    )


def test_connect_target_applies_status_color_to_the_status_bar() -> None:
    manager, *_ = _manager()
    spec = make_workspace_spec(status_color="#C05CE0")
    argv = manager.connect_target(spec, FACTS)
    assert argv[-1] == (
        "cd /app && exec env LC_ALL=C.UTF-8 LANG=C.UTF-8 tmux "
        "set -g status-style 'bg=#C05CE0,fg=#000000' \\; "
        "set -g status-left ' gswa-backend ' \\; set -g status-left-length 14 \\; "
        "new-session -A -s main bash -l"
    )


# --- status ------------------------------------------------------------------------


def test_status_all_reports_running_state() -> None:
    manager, *_ = _manager(container=FakeContainerAccess(running=True))
    statuses = manager.status_all([(SPEC, REMOTE)])
    assert len(statuses) == 1
    assert statuses[0].key == "gswa-backend"
    assert statuses[0].running is True


def test_status_all_reports_not_running_on_unreachable_host() -> None:
    class Unreachable(FakeContainerAccess):
        def read_facts(self, spec, remote):  # type: ignore[no-untyped-def]
            raise ConfigError("could not read devcontainer.json")

    manager, *_ = _manager(container=Unreachable())
    statuses = manager.status_all([(SPEC, REMOTE)])
    assert statuses[0].running is False


# --- ssh-config --------------------------------------------------------------------


def test_install_ssh_config_writes_conf_and_ensures_include() -> None:
    manager, _, _, cfg = _manager()
    block = SshConfigBlock(
        host_alias="gswa-devbox",
        host_ip="20.0.0.5",
        admin_user="azureuser",
        container_alias="gswa-container",
        container_port=2222,
        container_user="dev",
        host_key_alias="gswa-container",
    )
    path = manager.install_ssh_config([block])
    assert path.endswith("billet.conf")
    assert cfg.written is not None
    assert "Host gswa-container" in cfg.written
    assert cfg.include_calls == 1
