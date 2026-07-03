"""Tests for the CLI plan renderer — live checklist on a terminal, plain lines when piped.

``PlanRenderer`` is fed a scripted event sequence against a recording console: with
``force_terminal=True`` the Live checklist path runs; with a plain file the renderer must
degrade to one sequential log line per event with no ANSI live artifacts.
"""

import io

from rich.console import Console

from billet.cli._console import BILLET_THEME, PlanRenderer, planning_status
from billet.contracts import PlanStep, StepKind

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
