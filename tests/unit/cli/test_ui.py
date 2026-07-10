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

from billet.cli._ui import (
    BILLET_THEME,
    PlanRenderer,
    UIState,
    configure,
    planning_status,
    render_error,
)
from billet.contracts import PlanStep, StepKind
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
