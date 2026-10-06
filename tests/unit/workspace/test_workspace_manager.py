"""Tests for WorkspaceManager — plan composition, apply dispatch, connect argv, ssh-config."""

import pytest

from billet.access.doctor.ssh_doctor_access import SshDoctorAccess
from billet.contracts import (
    BerthFileState,
    ComposeReport,
    DevcontainerFacts,
    DoctorFilters,
    DoctorSkip,
    PortPublisher,
    RemoteHost,
    RunningBerthState,
    RuntimeState,
    SharedProject,
    SshConfigBlock,
    WorkspaceRuntimeRead,
    WorkspaceSpec,
    WorkspaceStepKind,
)
from billet.shared.errors import ConfigError, HostOperationError
from billet.workspace.manager.workspace_manager import WorkspaceManager
from tests.unit._fakes import (
    FakeContainerAccess,
    FakeDoctorAccess,
    FakeProcessRunner,
    FakeSourceAccess,
    FakeSshConfigAccess,
    RecordingPlanObserver,
    completed,
    make_container,
    make_devcontainer_facts,
    make_packaged_berth,
    make_remote_host,
    make_runtime_read,
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
    return WorkspaceManager(src, cont, cfg, FakeDoctorAccess()), src, cont, cfg


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


def test_apply_start_threads_claude_token_to_compose_up() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_start(SPEC, verify=False)
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="", claude_oauth_token="tok-1")
    assert container.claude_oauth_tokens == ["tok-1"]


def test_apply_start_passes_none_token_when_unset() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_start(SPEC, verify=False)
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="")
    assert container.claude_oauth_tokens == [None]


def test_apply_start_emits_started_then_succeeded_for_every_step_in_order() -> None:
    manager, *_ = _manager()
    plan = manager.plan_start(SPEC, verify=True)
    observer = RecordingPlanObserver()
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="", observer=observer)
    expected: list[tuple[str, object]] = []
    for step in plan.steps:
        expected += [("started", step), ("succeeded", step)]
    assert observer.events == expected


def test_apply_start_emits_the_verify_output_after_that_step_succeeded() -> None:
    container = FakeContainerAccess(verify_output="pytest 8.3.2\nruff 0.6.9")
    manager, *_ = _manager(container=container)
    plan = manager.plan_start(SPEC, verify=True)
    observer = RecordingPlanObserver()
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="", observer=observer)
    verify_step = plan.steps[-1]
    assert verify_step.kind is WorkspaceStepKind.VERIFY
    # The output event trails the step's own success — the client ticks the row, then shows
    # what the command printed, verbatim.
    assert observer.events[-2:] == [("succeeded", verify_step), ("output", verify_step)]
    assert observer.outputs == ["pytest 8.3.2\nruff 0.6.9"]


def test_apply_start_emits_no_output_event_when_the_step_printed_nothing() -> None:
    manager, _, container, _ = _manager()  # the default fake's verify_cmd prints nothing
    plan = manager.plan_start(SPEC, verify=True)
    observer = RecordingPlanObserver()
    manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="", observer=observer)
    assert "verify" in container.calls  # the step ran; it simply had nothing to report
    assert not any(event == "output" for event, _ in observer.events)
    assert observer.outputs == []


def test_apply_start_emits_failed_reraises_and_runs_no_later_steps() -> None:
    class ExplodingContainer(FakeContainerAccess):
        def run_post_create(
            self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
        ) -> None:
            super().run_post_create(spec, remote, facts)
            raise ConfigError("boom")

    container = ExplodingContainer()
    manager, *_ = _manager(container=container)
    plan = manager.plan_start(SPEC, verify=True)  # ensure_source, compose_up, post_create, verify
    observer = RecordingPlanObserver()
    with pytest.raises(ConfigError, match="boom"):
        manager.apply_start(plan, SPEC, REMOTE, personal_bootstrap_cmd="", observer=observer)
    assert observer.events == [
        ("started", plan.steps[0]),
        ("succeeded", plan.steps[0]),
        ("started", plan.steps[1]),
        ("succeeded", plan.steps[1]),
        ("started", plan.steps[2]),
        ("failed", plan.steps[2]),
    ]
    assert "verify" not in container.calls


# --- stop --------------------------------------------------------------------------


def test_apply_stop_reads_facts_then_stops() -> None:
    manager, _, container, _ = _manager()
    plan = manager.plan_stop(SPEC)
    manager.apply_stop(plan, SPEC, REMOTE)
    assert container.calls == ["read_facts", "compose_stop"]


def test_apply_stop_emits_started_then_succeeded_for_the_stop_step() -> None:
    manager, *_ = _manager()
    plan = manager.plan_stop(SPEC)
    observer = RecordingPlanObserver()
    manager.apply_stop(plan, SPEC, REMOTE, observer)
    step = plan.steps[0]
    assert observer.events == [("started", step), ("succeeded", step)]


def test_apply_stop_emits_failed_and_reraises_when_stop_raises() -> None:
    class ExplodingContainer(FakeContainerAccess):
        def compose_stop(
            self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
        ) -> None:
            super().compose_stop(spec, remote, facts)
            raise ConfigError("boom")

    manager, *_ = _manager(container=ExplodingContainer())
    plan = manager.plan_stop(SPEC)
    observer = RecordingPlanObserver()
    with pytest.raises(ConfigError, match="boom"):
        manager.apply_stop(plan, SPEC, REMOTE, observer)
    step = plan.steps[0]
    assert observer.events == [("started", step), ("failed", step)]


# --- connect -----------------------------------------------------------------------


def test_connect_target_builds_tty_tmux_argv_through_the_container_alias() -> None:
    manager, *_ = _manager()
    argv = manager.connect_target(SPEC, FACTS)
    assert argv[0] == "ssh"
    assert "-t" in argv
    assert "gswa-container" in argv  # via the alias (no user@host)
    assert argv[-1] == (
        "cd /app && exec env LC_ALL=C.UTF-8 LANG=C.UTF-8 TERM=xterm-256color tmux "
        "set -g @billet_workspace gswa-backend \\; set -g @billet_host devbox \\; "
        "new-session -A -s main bash -l"
    )


def test_connect_target_publishes_status_color_as_a_user_option() -> None:
    manager, *_ = _manager()
    spec = make_workspace_spec(status_color="#C05CE0")
    argv = manager.connect_target(spec, FACTS)
    assert argv[-1] == (
        "cd /app && exec env LC_ALL=C.UTF-8 LANG=C.UTF-8 TERM=xterm-256color tmux "
        "set -g @billet_workspace gswa-backend \\; set -g @billet_host devbox \\; "
        "set -g @billet_color '#C05CE0' \\; "
        "new-session -A -s main bash -l"
    )


def test_connect_target_writes_no_tmux_presentation_option() -> None:
    # ADR-0008 rule 2: presentation belongs to the operator's adopted tmux config.
    manager, *_ = _manager()
    remote_command = manager.connect_target(make_workspace_spec(status_color="#C05CE0"), FACTS)[-1]
    for option in ("status-style", "status-left", "status-right", "status-format", "window-status"):
        assert option not in remote_command


# --- status ------------------------------------------------------------------------


def test_status_all_reports_running_state() -> None:
    manager, *_ = _manager(container=FakeContainerAccess(running=True))
    statuses = manager.status_all([(SPEC, REMOTE)])
    assert len(statuses) == 1
    assert statuses[0].key == "gswa-backend"
    assert statuses[0].running is True
    assert statuses[0].reachable is True


def test_status_all_reports_not_running_when_repo_missing() -> None:
    # A reachable host without the repo cloned is "stopped", not "unreachable".
    class RepoMissing(FakeContainerAccess):
        def read_facts(self, spec, remote):  # type: ignore[no-untyped-def]
            raise ConfigError("could not read devcontainer.json")

    manager, *_ = _manager(container=RepoMissing())
    statuses = manager.status_all([(SPEC, REMOTE)])
    assert statuses[0].running is False
    assert statuses[0].reachable is True


def test_status_all_reports_unreachable_host_distinctly() -> None:
    class Unreachable(FakeContainerAccess):
        def read_facts(self, spec, remote):  # type: ignore[no-untyped-def]
            raise HostOperationError("could not reach 20.0.0.5 over SSH")

    manager, *_ = _manager(container=Unreachable())
    statuses = manager.status_all([(SPEC, REMOTE)])
    assert statuses[0].running is False
    assert statuses[0].reachable is False


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


# --- doctor ------------------------------------------------------------------------

DEVBOX = make_remote_host(ip="gswa-devbox")
OTHER = make_remote_host(ip="other-box")
BILLET_WS = make_workspace_spec(key="billet", repo_dir="billet", container_ssh_port=2223)
SQUADRA_WS = make_workspace_spec(key="squadra", host="other", repo_dir="squadra")
DOCTOR_ITEMS = [(SPEC, DEVBOX), (SQUADRA_WS, OTHER), (BILLET_WS, DEVBOX)]


def _doctor_manager(doctor: FakeDoctorAccess) -> WorkspaceManager:
    return WorkspaceManager(
        FakeSourceAccess(), FakeContainerAccess(), FakeSshConfigAccess(), doctor
    )


def test_doctor_probes_each_host_once_with_all_its_workspaces() -> None:
    doctor = FakeDoctorAccess()
    report = _doctor_manager(doctor).doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters())
    assert doctor.calls == [
        ("gswa-devbox", ("gswa-backend", "billet")),
        ("other-box", ("squadra",)),
    ]
    assert [(s.host, s.workspace) for s in report.statuses] == [
        ("devbox", "gswa-backend"),
        ("devbox", "billet"),
        ("other", "squadra"),
    ]
    assert report.skipped == ()
    assert report.berth_version == make_packaged_berth().version


def test_doctor_reports_drift_per_workspace() -> None:
    doctor = FakeDoctorAccess(overrides={"billet": {"sshd.conf": "Port 2200\n"}})
    report = _doctor_manager(doctor).doctor(
        [(SPEC, DEVBOX), (BILLET_WS, DEVBOX)], make_packaged_berth(), DoctorFilters()
    )
    states = {s.workspace: {f.file: f.state for f in s.files} for s in report.statuses}
    assert states["gswa-backend"]["sshd.conf"] is BerthFileState.OK
    assert states["billet"]["sshd.conf"] is BerthFileState.DRIFT


def test_doctor_filters_by_host_and_workspace() -> None:
    doctor = FakeDoctorAccess()
    manager = _doctor_manager(doctor)
    by_host = manager.doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters(host="other"))
    assert [s.workspace for s in by_host.statuses] == ["squadra"]
    by_ws = manager.doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters(workspace="billet"))
    assert [s.workspace for s in by_ws.statuses] == ["billet"]
    assert doctor.calls[-1] == ("gswa-devbox", ("billet",))


def test_doctor_skips_an_unreachable_host_and_reads_the_rest() -> None:
    doctor = FakeDoctorAccess(unreachable=["gswa-devbox"], failing=[])
    report = _doctor_manager(doctor).doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters())
    assert report.skipped == (
        DoctorSkip(host="devbox", reason="unreachable", workspaces=("gswa-backend", "billet")),
    )
    assert [s.workspace for s in report.statuses] == ["squadra"]


def test_doctor_skips_a_host_whose_probe_fails_with_the_reason() -> None:
    doctor = FakeDoctorAccess(failing=["other-box"])
    report = _doctor_manager(doctor).doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters())
    (skip,) = report.skipped
    assert skip.host == "other"
    assert skip.reason.startswith("probe failed: command failed (exit 1)")


def test_doctor_skips_a_host_whose_probe_times_out_as_its_own_reason() -> None:
    """D-A7-3: typed, not the generic `probe failed: command failed (exit -1): ssh …`."""
    doctor = FakeDoctorAccess(timing_out=["other-box"])
    report = _doctor_manager(doctor).doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters())
    assert report.skipped == (
        DoctorSkip(host="other", reason="probe timed out after 30s", workspaces=("squadra",)),
    )
    assert [s.workspace for s in report.statuses] == ["gswa-backend", "billet"]


def test_doctor_over_the_real_access_makes_exactly_one_ssh_call_per_host() -> None:
    runner = FakeProcessRunner(lambda _argv: completed(stdout=""))
    manager = WorkspaceManager(
        FakeSourceAccess(), FakeContainerAccess(), FakeSshConfigAccess(), SshDoctorAccess(runner)
    )
    report = manager.doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters())
    assert len(runner.calls) == 2
    assert [call[-2] for call in runner.calls] == ["azureuser@gswa-devbox", "azureuser@other-box"]
    assert len(report.statuses) == 3


def test_doctor_attaches_each_workspace_runtime_against_its_checkout_stamp() -> None:
    doctor = FakeDoctorAccess(
        overrides={"billet": {"berth.version": "2\n"}},
        runtimes={
            "billet": make_runtime_read("dev-entrypoint: berth=1", project="billet"),
            "squadra": WorkspaceRuntimeRead(RuntimeState.NOT_RUNNING),
        },
    )
    report = _doctor_manager(doctor).doctor(DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters())
    runtime = {s.workspace: s.runtime for s in report.statuses}
    gswa, billet, squadra = runtime["gswa-backend"], runtime["billet"], runtime["squadra"]
    assert gswa is not None and gswa.berth_state is RunningBerthState.MATCH
    assert billet is not None and billet.berth_state is RunningBerthState.DIFFERS
    assert (billet.running_berth, billet.checkout_stamp) == ("1", 2)
    assert squadra is not None and squadra.state is RuntimeState.NOT_RUNNING


# --- doctor: compose project and published ports (A8) -------------------------------------

BRAND_WS = make_workspace_spec(key="genshift-brand", repo_dir="genshift-brand")


def test_doctor_reports_each_running_workspace_on_its_own_project() -> None:
    report = _doctor_manager(FakeDoctorAccess()).doctor(
        DOCTOR_ITEMS, make_packaged_berth(), DoctorFilters()
    )
    compose = {s.workspace: s.runtime.compose for s in report.statuses if s.runtime}
    assert compose == {
        "gswa-backend": ComposeReport(projects=("gswa-backend",)),
        "billet": ComposeReport(projects=("billet",)),
        "squadra": ComposeReport(projects=("squadra",)),
    }
    assert report.shared_projects == ()


def test_doctor_reports_a_project_two_workspaces_share_once_per_host() -> None:
    """S2-5 through the manager: one Host-level finding; the stopped one contributes nothing."""
    billet = make_container("devcontainer", "billet")
    brand = make_container("devcontainer", "genshift-brand")
    sql = make_container("devcontainer", "sql", PortPublisher("0.0.0.0", 5432, 5432, "tcp"))
    doctor = FakeDoctorAccess(
        runtimes={
            "billet": make_runtime_read(containers=[billet, brand, sql]),
            "genshift-brand": make_runtime_read(containers=[brand, billet, sql]),
            "gswa-backend": WorkspaceRuntimeRead(RuntimeState.NOT_RUNNING),
        }
    )
    items = [(SPEC, DEVBOX), (BILLET_WS, DEVBOX), (BRAND_WS, DEVBOX), (SQUADRA_WS, OTHER)]
    report = _doctor_manager(doctor).doctor(items, make_packaged_berth(), DoctorFilters())
    assert report.shared_projects == (
        SharedProject("devbox", "devcontainer", ("billet", "genshift-brand")),
    )
    runtime = {s.workspace: s.runtime for s in report.statuses}
    gswa, billet_rt, brand_rt = (
        runtime["gswa-backend"],
        runtime["billet"],
        runtime["genshift-brand"],
    )
    assert gswa is not None and gswa.compose is None  # not running: neither line
    assert billet_rt is not None and billet_rt.compose is not None
    assert brand_rt is not None and brand_rt.compose is not None
    assert billet_rt.compose.foreign_projects == ("devcontainer",)
    # S2-6: the sidecar both `ps` list is reported once, under the first Workspace.
    assert [e.service for e in billet_rt.compose.exposed] == ["sql"]
    assert brand_rt.compose.exposed == ()


def test_doctor_never_groups_projects_across_hosts() -> None:
    """Two Hosts may each run a project of the same name; that is not a shared project."""
    doctor = FakeDoctorAccess(
        runtimes={
            "gswa-backend": make_runtime_read(project="devcontainer"),
            "squadra": make_runtime_read(project="devcontainer"),
        }
    )
    items = [(SPEC, DEVBOX), (SQUADRA_WS, OTHER)]
    report = _doctor_manager(doctor).doctor(items, make_packaged_berth(), DoctorFilters())
    assert report.shared_projects == ()
