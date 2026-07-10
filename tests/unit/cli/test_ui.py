"""Tests for the CLI presentation layer (``_ui``) — plan renderer and the error view.

``PlanRenderer`` is fed a scripted event sequence against a recording console: with
``force_terminal=True`` the Live checklist path runs; with a plain file the renderer must
degrade to one sequential log line per event with no ANSI live artifacts. The error view
renders to a plain console so the raw copy is asserted directly.
"""

from collections.abc import Iterator
import io

import pytest
from rich.console import Console

from billet.cli import _ui
from billet.cli._ui import (
    BILLET_THEME,
    PlanRenderer,
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

_STEPS = (
    PlanStep(StepKind.START, "start VM devbox"),
    PlanStep(StepKind.WAIT_REACHABLE, "wait for SSH on devbox"),
    PlanStep(StepKind.ENSURE_SUPPLY_CHAIN, "install Docker (base supply chain)"),
)


def _terminal_console() -> Console:
    return Console(theme=BILLET_THEME, record=True, force_terminal=True, width=80)


def test_terminal_checklist_marks_succeeded_steps() -> None:
    console = _terminal_console()
    with PlanRenderer(_STEPS, console=console) as renderer:
        renderer.step_started(_STEPS[0])
        renderer.step_succeeded(_STEPS[0])
        renderer.step_started(_STEPS[1])
        renderer.step_succeeded(_STEPS[1])
    text = console.export_text()
    assert "✓ start VM devbox" in text
    assert "✓ wait for SSH on devbox" in text
    assert "● install Docker (base supply chain)" in text  # never started -> stays pending


def test_terminal_checklist_marks_a_failed_step() -> None:
    console = _terminal_console()
    with PlanRenderer(_STEPS, console=console) as renderer:
        renderer.step_started(_STEPS[0])
        renderer.step_succeeded(_STEPS[0])
        renderer.step_started(_STEPS[1])
        renderer.step_failed(_STEPS[1])
    text = console.export_text()
    assert "✓ start VM devbox" in text
    assert "✗ wait for SSH on devbox" in text


def test_non_terminal_degrades_to_plain_sequential_lines() -> None:
    buffer = io.StringIO()
    console = Console(theme=BILLET_THEME, file=buffer, force_terminal=False, width=200)
    with PlanRenderer(_STEPS, console=console) as renderer:
        renderer.step_started(_STEPS[0])
        renderer.step_succeeded(_STEPS[0])
        renderer.step_started(_STEPS[1])
        renderer.step_failed(_STEPS[1])
    output = buffer.getvalue()
    lines = [line for line in output.splitlines() if line]
    assert lines == [
        "[billet] … start VM devbox",
        "[billet] ✓ start VM devbox",
        "[billet] … wait for SSH on devbox",
        "[billet] ✗ wait for SSH on devbox",
    ]
    assert "\x1b[" not in output  # no ANSI live artifacts in piped output


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
