"""Tests for the CLI presentation layer (``_ui``) — the phase checklist and static views.

``PhaseChecklist`` is fed a scripted PlanObserver event sequence against a recording
console: with ``force_terminal=True`` the Live path runs; with a plain file it must
degrade to one completion line per phase with no ANSI live artifacts; under ``--quiet``
it stays silent. The error view and static renderables print to a plain console so the
raw copy is asserted directly.
"""

from collections.abc import Iterator
import io
import json

import pytest
from rich.console import Console

from billet.cli import _ui
from billet.cli._ui import (
    BILLET_THEME,
    PhaseChecklist,
    UIState,
    configure,
    planning_status,
    render_error,
)
from billet.contracts import (
    Plan,
    PlanStep,
    StepKind,
    WorkspacePlan,
    WorkspacePlanStep,
    WorkspaceStepKind,
)
from billet.shared.errors import (
    AzLoginRequired,
    BilletError,
    ConfigError,
    HostOperationError,
    ProcessError,
)
from tests.unit._fakes import make_host_spec, make_workspace_spec

_START = PlanStep(StepKind.START, "start VM gswa-devbox")
_WAIT = PlanStep(StepKind.WAIT_REACHABLE, "wait for SSH on gswa-devbox")
_COMPOSE = WorkspacePlanStep(WorkspaceStepKind.COMPOSE_UP, "docker compose up -d --build")


def _terminal_console() -> Console:
    return Console(theme=BILLET_THEME, record=True, force_terminal=True, width=80)


def _checklist_phases() -> list[_ui.Phase]:
    return [
        _ui.Phase(key="host:start", label="start vm gswa-devbox", group="host"),
        _ui.Phase(key="host:wait_reachable", label="wait for ssh", group="host"),
        _ui.Phase(
            key="workspace:compose_up",
            label="docker compose up · build",
            group="workspace",
            bar=True,
        ),
    ]


def test_checklist_tty_renders_title_groups_and_states() -> None:
    console = _terminal_console()
    checklist = PhaseChecklist(_checklist_phases(), title="posting api → devbox", console=console)
    with checklist:
        checklist.step_started(_START)
        checklist.step_succeeded(_START)
        checklist.step_started(_WAIT)
    text = console.export_text()
    assert "posting api → devbox" in text
    assert "↳" in text  # the running total
    assert "✓ start vm gswa-devbox" in text
    assert "0:00" in text  # the frozen elapsed for the done row
    assert "○ docker compose up · build" in text  # never started -> stays pending
    assert "host" in text and "workspace" in text  # group rules for the combined arc


def test_checklist_tty_marks_a_failed_phase() -> None:
    console = _terminal_console()
    checklist = PhaseChecklist(_checklist_phases(), title="posting api → devbox", console=console)
    with checklist:
        checklist.step_started(_START)
        checklist.step_failed(_START)
    assert "✗ start vm gswa-devbox" in console.export_text()


def test_checklist_piped_prints_one_completion_line_per_phase() -> None:
    buffer = io.StringIO()
    console = Console(theme=BILLET_THEME, file=buffer, force_terminal=False, width=200)
    checklist = PhaseChecklist(_checklist_phases(), title="posting api → devbox", console=console)
    with checklist:
        checklist.step_started(_START)
        checklist.step_succeeded(_START)
        checklist.step_started(_WAIT)
        checklist.step_failed(_WAIT)
    output = buffer.getvalue()
    lines = [line for line in output.splitlines() if line]
    assert lines == [
        "  start vm gswa-devbox … ok",
        "  wait for ssh … failed",
    ]
    assert "\x1b[" not in output  # no ANSI live artifacts in piped output


def test_checklist_quiet_is_silent(reset_ui_state: None) -> None:
    configure(UIState(quiet=True))
    console = _terminal_console()
    checklist = PhaseChecklist(_checklist_phases(), title="posting api → devbox", console=console)
    with checklist:
        checklist.step_started(_START)
        checklist.step_succeeded(_START)
    assert console.export_text() == ""


def test_checklist_ignores_unknown_steps() -> None:
    buffer = io.StringIO()
    console = Console(theme=BILLET_THEME, file=buffer, force_terminal=False, width=200)
    checklist = PhaseChecklist(_checklist_phases(), title="t", console=console)
    stranger = PlanStep(StepKind.CREATE, "create resource group + VM x")
    with checklist:
        checklist.step_started(stranger)
        checklist.step_succeeded(stranger)
    assert buffer.getvalue() == ""


def test_checklist_total_elapsed_formats_mss() -> None:
    phase = _ui.Phase(key="host:start", label="start vm x")
    phase.t0 = 0.0
    phase.t1 = 75.0
    console, _ = _plain_console()
    checklist = PhaseChecklist([phase], title="t", console=console)
    assert checklist.total_elapsed() == "1:15"


def test_host_phases_carry_appendix_a_labels() -> None:
    spec = make_host_spec(vm_name="gswa-devbox")
    plan = Plan(
        host_key="devbox",
        steps=(
            PlanStep(StepKind.CREATE, "create resource group + VM gswa-devbox", billable=True),
            PlanStep(StepKind.PIN_INBOUND, "pin inbound SSH"),
            PlanStep(StepKind.WAIT_REACHABLE, "wait for SSH"),
            PlanStep(StepKind.ENSURE_SUPPLY_CHAIN, "install Docker"),
        ),
    )
    phases = _ui.host_phases(plan, spec)
    assert [phase.label for phase in phases] == [
        "create vm gswa-devbox",
        "pin inbound ssh",
        "wait for ssh",
        "install docker",
    ]
    assert all(phase.group == "host" for phase in phases)


def test_workspace_phases_carry_labels_and_the_compose_bar() -> None:
    spec = make_workspace_spec(repo_url="git@github.com:genshift/api.git")
    plan = WorkspacePlan(
        workspace_key="api",
        steps=(
            WorkspacePlanStep(WorkspaceStepKind.ENSURE_SOURCE, "clone/fetch"),
            WorkspacePlanStep(WorkspaceStepKind.COMPOSE_UP, "docker compose up -d --build"),
            WorkspacePlanStep(WorkspaceStepKind.PERSONAL_BOOTSTRAP, "run personal bootstrap"),
            WorkspacePlanStep(WorkspaceStepKind.POST_CREATE, "run postCreate"),
        ),
    )
    phases = _ui.workspace_phases(plan, spec)
    assert [phase.label for phase in phases] == [
        "clone / fast-forward genshift/api",
        "docker compose up · build",
        "personal bootstrap",
        "postCreate",
    ]
    assert [phase.bar for phase in phases] == [False, True, False, False]
    assert all(phase.group == "workspace" for phase in phases)


def test_planning_status_shows_spinner_text_on_a_terminal() -> None:
    console = _terminal_console()
    with planning_status(console):
        pass  # the first frame paints synchronously — no refresh-thread timing to wait on
    assert "planning" in console.export_text()


def test_planning_status_is_silent_when_not_a_terminal() -> None:
    buffer = io.StringIO()
    console = Console(theme=BILLET_THEME, file=buffer, force_terminal=False, width=200)
    with planning_status(console):
        pass
    assert buffer.getvalue() == ""


# --- error view (§6.6) ---------------------------------------------------------------


@pytest.fixture
def reset_ui_state() -> Iterator[None]:
    yield
    configure(UIState())


def _plain_console() -> tuple[Console, io.StringIO]:
    buffer = io.StringIO()
    return Console(theme=BILLET_THEME, file=buffer, force_terminal=False, width=200), buffer


def test_render_error_az_login_required_names_the_next_command() -> None:
    console, buffer = _plain_console()
    render_error(AzLoginRequired("no token"), console)
    output = buffer.getvalue()
    assert "✗ az login required" in output
    assert "billet can't reach azure." in output
    assert "az login" in output


def test_render_error_config_error_keeps_the_validation_message() -> None:
    console, buffer = _plain_console()
    render_error(ConfigError("config file not found: /tmp/nope.toml"), console)
    output = buffer.getvalue()
    assert "✗ config error" in output
    assert "config file not found: /tmp/nope.toml" in output
    assert "edit it, then retry" in output


def test_render_error_host_operation_splits_headline_and_remediation() -> None:
    console, buffer = _plain_console()
    render_error(
        HostOperationError(
            "VM gswa-devbox is 'stopped' (not deallocated). "
            "Start it manually or deallocate it, then retry."
        ),
        console,
    )
    output = buffer.getvalue()
    assert "✗ VM gswa-devbox is 'stopped' (not deallocated)" in output
    assert "Start it manually or deallocate it, then retry." in output


def test_render_error_host_operation_single_sentence_is_just_the_headline() -> None:
    console, buffer = _plain_console()
    render_error(HostOperationError("VM gswa-devbox does not exist — nothing to stop."), console)
    lines = [line for line in buffer.getvalue().splitlines() if line.strip()]
    assert lines == ["✗ VM gswa-devbox does not exist — nothing to stop."]


def test_render_error_process_error_shows_only_the_stderr_tail() -> None:
    stderr = "\n".join(f"line {n}" for n in range(1, 9))
    console, buffer = _plain_console()
    render_error(ProcessError(("ssh", "devbox", "compose"), 1, stderr), console)
    output = buffer.getvalue()
    assert "✗ ssh failed · exit 1" in output
    assert "↓ last output" in output
    assert "line 3" not in output  # only the last 5 of 8 lines survive
    assert "line 4" in output
    assert "line 8" in output
    assert "retry with --verbose" in output


def test_render_error_process_error_verbose_shows_argv_and_full_output(
    reset_ui_state: None,
) -> None:
    configure(UIState(verbose=True))
    stderr = "\n".join(f"line {n}" for n in range(1, 9))
    console, buffer = _plain_console()
    render_error(ProcessError(("ssh", "devbox", "compose"), 1, stderr), console)
    output = buffer.getvalue()
    assert "ssh devbox compose" in output  # full argv under -v
    assert "line 1" in output  # full stderr under -v
    assert "retry with --verbose" not in output


def test_render_error_generic_billet_error_is_the_headline() -> None:
    console, buffer = _plain_console()
    render_error(BilletError("something went sideways"), console)
    output = buffer.getvalue()
    assert "✗ something went sideways" in output
    assert "Traceback" not in output


# --- plan view (§6.4) ------------------------------------------------------------------


def test_render_host_plan_cold_create_header_rows_and_billable_tag() -> None:
    plan = Plan(
        host_key="devbox",
        steps=(
            PlanStep(
                StepKind.CREATE,
                "create resource group + VM gswa-devbox (Standard_D4s_v4, BILLABLE)",
                billable=True,
            ),
            PlanStep(StepKind.WAIT_REACHABLE, "wait for SSH on gswa-devbox"),
        ),
    )
    console, buffer = _plain_console()
    _ui.render_host_plan(plan, console=console)
    output = buffer.getvalue()
    assert "plan · host devbox · cold create" in output
    assert "1" in output and "2" in output
    assert "create resource group + vm gswa-devbox (standard_d4s_v4)" in output
    assert "! billable" in output
    assert "BILLABLE" not in output


def test_render_host_plan_resume_mode() -> None:
    plan = Plan(
        host_key="devbox",
        steps=(PlanStep(StepKind.START, "start VM gswa-devbox"),),
    )
    console, buffer = _plain_console()
    _ui.render_host_plan(plan, console=console)
    assert "plan · host devbox · resume" in buffer.getvalue()


def test_render_host_plan_empty_names_the_state() -> None:
    console, buffer = _plain_console()
    _ui.render_host_plan(Plan(host_key="devbox", steps=()), already="deallocated", console=console)
    assert "· nothing to do — devbox already deallocated" in buffer.getvalue()


def test_render_workspace_plan_numbers_rows() -> None:
    plan = WorkspacePlan(
        workspace_key="api",
        steps=(
            WorkspacePlanStep(WorkspaceStepKind.ENSURE_SOURCE, "clone/fetch repo"),
            WorkspacePlanStep(WorkspaceStepKind.COMPOSE_UP, "docker compose up -d --build"),
        ),
    )
    console, buffer = _plain_console()
    _ui.render_workspace_plan(plan, console=console)
    output = buffer.getvalue()
    assert "plan · workspace api · start" in output
    assert "docker compose up -d --build" in output


# --- ls view (§6.5) ----------------------------------------------------------------------


def _ls_groups() -> list[_ui.LsHostGroup]:
    return [
        _ui.LsHostGroup(
            key="devbox",
            vm_size="Standard_D4s_v4",
            manages_workspaces=True,
            rows=(
                _ui.LsWorkspaceRow(key="api", state="running", alias="api.devbox", port=2222),
                _ui.LsWorkspaceRow(key="web", state="stopped", alias="web.devbox", port=2224),
            ),
        ),
        _ui.LsHostGroup(key="fleet", vm_size="Standard_D4s_v5", manages_workspaces=False, rows=()),
    ]


def test_render_ls_piped_prints_fixed_greppable_columns() -> None:
    console, buffer = _plain_console()
    _ui.render_ls(_ls_groups(), console=console)
    lines = [line for line in buffer.getvalue().splitlines() if line]
    assert lines[0].split() == ["HOST", "WORKSPACE", "STATE", "PORT"]
    assert lines[1].split() == ["devbox", "api", "running", "2222"]
    assert lines[2].split() == ["devbox", "web", "stopped", "2224"]
    assert "●" not in buffer.getvalue()
    assert "█" not in buffer.getvalue()


def test_render_ls_tty_groups_hosts_and_marks_states() -> None:
    console = _terminal_console()
    _ui.render_ls(_ls_groups(), console=console)
    text = console.export_text()
    assert "devbox" in text
    assert "standard_d4s_v4" in text  # vm size lowercased per the voice
    assert "● api" not in text  # glyph and key live in separate aligned cells
    assert "●" in text and "○" in text  # state dots for running and stopped
    assert ":2222" in text
    assert "manages no workspaces" in text  # the non-managing host header
    assert "2 / 6" in text  # posted / rack total


def test_render_ls_tty_renders_placeholder_for_a_host_without_vm_size() -> None:
    # An adopted host's table may omit vm_size entirely; the rack header shows a dash.
    console = _terminal_console()
    groups = [
        _ui.LsHostGroup(
            key="adopted",
            vm_size=None,
            manages_workspaces=True,
            rows=(_ui.LsWorkspaceRow(key="api", state="running", alias="api.a", port=2222),),
        )
    ]
    _ui.render_ls(groups, console=console)
    text = console.export_text()
    assert "adopted   —" in text
    assert "None" not in text


def test_render_ls_unreachable_hint_piped() -> None:
    console, buffer = _plain_console()
    groups = [
        _ui.LsHostGroup(
            key="devbox",
            vm_size="Standard_D4s_v4",
            manages_workspaces=True,
            rows=(
                _ui.LsWorkspaceRow(key="api", state="unreachable", alias="api.devbox", port=2222),
            ),
        )
    ]
    _ui.render_ls(groups, console=console)
    output = buffer.getvalue()
    assert "unreachable" in output
    assert "billet host up --host devbox" in output


# --- empty state + banner ----------------------------------------------------------------


def test_empty_state_shows_rack_on_tty_and_plain_lines_piped() -> None:
    lines = ("no workspaces yet.", "declare [workspaces.<key>] in config.toml,")
    tty = _terminal_console()
    _ui.empty_state(lines, console=tty)
    assert "░░ ░░" in tty.export_text()
    plain, buffer = _plain_console()
    _ui.empty_state(lines, console=plain)
    assert "░░" not in buffer.getvalue()
    assert "no workspaces yet." in buffer.getvalue()


def test_banner_rack_on_tty_one_liner_piped() -> None:
    tty = _terminal_console()
    _ui.banner("0.4.0", console=tty)
    text = tty.export_text()
    assert "██ ░░   billet 0.4.0" in text
    assert "a berth for every repo" in text
    plain, buffer = _plain_console()
    _ui.banner("0.4.0", console=plain)
    assert buffer.getvalue() == "billet 0.4.0\n"


# --- compose-up streaming (phase 4) -------------------------------------------------------


@pytest.mark.parametrize(
    ("line", "fraction"),
    [
        ("#9 [build 5/9] COPY requirements.txt .", 5 / 9),
        ("#5 [2/7] RUN pip install -r requirements.txt", 2 / 7),
        ("#12 [stage-1  4/4] WORKDIR /app", 1.0),  # BuildKit pads stage names for alignment
        ("#9 0.482 Collecting rich", None),  # build output, not a step marker
        ("Step 3/9 : COPY . .", None),  # legacy (non-BuildKit) format is not parsed
        ("#3 [internal] load metadata", None),
    ],
)
def test_buildkit_fraction_parses_step_markers(line: str, fraction: float | None) -> None:
    assert _ui.buildkit_fraction(line) == fraction


def test_compose_tail_feeds_log_and_progress() -> None:
    console = _terminal_console()
    phases = [
        _ui.Phase(
            key=_ui.COMPOSE_UP_KEY, label="docker compose up · build", group="workspace", bar=True
        )
    ]
    checklist = PhaseChecklist(phases, title="posting api → devbox", console=console)
    feed = checklist.compose_tail()
    with checklist:
        checklist.step_started(_COMPOSE)
        feed("#9 [build 5/9] COPY requirements.txt .")
    text = console.export_text()
    assert "#9 [build 5/9] COPY requirements.txt ." in text  # the log tail renders
    assert phases[0].progress == 5 / 9  # and the bar went determinate


def test_compose_tail_ignores_blank_lines() -> None:
    console, _ = _plain_console()
    phases = [
        _ui.Phase(
            key=_ui.COMPOSE_UP_KEY, label="docker compose up · build", group="workspace", bar=True
        )
    ]
    checklist = PhaseChecklist(phases, title="t", console=console)
    feed = checklist.compose_tail()
    feed("#9 [build 5/9] COPY requirements.txt .")
    feed("   ")
    assert phases[0].last_line == "#9 [build 5/9] COPY requirements.txt ."


# --- phase 5: ascii fallback + verbose interleave ------------------------------------------


def test_ascii_flag_switches_the_glyph_set(reset_ui_state: None) -> None:
    configure(UIState(ascii_only=True))
    glyph_set = _ui.glyphs()
    assert glyph_set.done == "[ok]"
    assert glyph_set.spinner == "simpleDots"


def test_ascii_success_and_error_lines(reset_ui_state: None) -> None:
    configure(UIState(ascii_only=True))
    console, buffer = _plain_console()
    _ui.success("host devbox is up", "0:12", console=console)
    render_error(BilletError("boom"), console)
    output = buffer.getvalue()
    assert "[ok] host devbox is up - 0:12" in output
    assert "[x] boom" in output
    assert "✓" not in output and "✗" not in output


def test_ascii_banner_uses_hash_rack(reset_ui_state: None) -> None:
    configure(UIState(ascii_only=True))
    console = _terminal_console()
    _ui.banner("0.4.0", console=console)
    text = console.export_text()
    assert "## ..   billet 0.4.0" in text
    assert "█" not in text


def test_ascii_checklist_piped_completion_lines(reset_ui_state: None) -> None:
    configure(UIState(ascii_only=True))
    buffer = io.StringIO()
    console = Console(theme=BILLET_THEME, file=buffer, force_terminal=False, width=200)
    checklist = PhaseChecklist(_checklist_phases(), title="t", console=console)
    with checklist:
        checklist.step_started(_START)
        checklist.step_succeeded(_START)
    assert "start vm gswa-devbox … ok" in buffer.getvalue()


def test_verbose_checklist_prints_phase_headers_and_raw_lines(reset_ui_state: None) -> None:
    configure(UIState(verbose=True))
    console = _terminal_console()
    phases = [
        _ui.Phase(
            key=_ui.COMPOSE_UP_KEY, label="docker compose up · build", group="workspace", bar=True
        )
    ]
    checklist = PhaseChecklist(phases, title="posting api → devbox", console=console)
    feed = checklist.compose_tail()
    with checklist:
        checklist.step_started(_COMPOSE)
        feed("#9 [build 5/9] COPY requirements.txt .")
        checklist.step_succeeded(_COMPOSE)
    text = console.export_text()
    assert "» docker compose up · build" in text  # the per-phase header
    assert "#9 [build 5/9] COPY requirements.txt ." in text  # raw output interleaved
    assert "docker compose up · build … ok" in text  # plain completion line (no Live)
    assert phases[0].progress is None  # -v streams raw; the bar is not driven


def test_quiet_suppresses_hint_lines(reset_ui_state: None) -> None:
    configure(UIState(quiet=True))
    console, buffer = _plain_console()
    _ui.next_hint("billet ssh-config", console=console)
    _ui.hint("start it", "billet start api", console=console)
    assert buffer.getvalue() == ""
    _ui.info("aborted — no changes made", console=console)  # outcomes still print
    assert "aborted" in buffer.getvalue()


def test_render_ls_json_is_machine_readable() -> None:
    console, buffer = _plain_console()
    _ui.render_ls_json(_ls_groups(), console=console)
    records = json.loads(buffer.getvalue())
    assert records == [
        {"host": "devbox", "key": "api", "state": "running", "alias": "api.devbox", "port": 2222},
        {"host": "devbox", "key": "web", "state": "stopped", "alias": "web.devbox", "port": 2224},
    ]
