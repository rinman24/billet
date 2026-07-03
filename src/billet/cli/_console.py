"""Rich console theming and live plan-progress rendering for the billet CLI.

The only module in the codebase that imports ``rich``: all presentation stays in
``billet.cli`` (managers and access never print — ADR-0001), and the tool's look is
centralised here so restyling is a one-file change.
"""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from enum import Enum
from types import TracebackType
from typing import Self

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.theme import Theme

from billet.contracts import PlanStep, WorkspacePlanStep

# Semantic style names for plan progress. Placeholder colors for now — the billet brand
# colors are swapped here (and only here) when the palette lands.
BILLET_THEME = Theme(
    {
        "billet.pending": "dim",
        "billet.running": "bold cyan",
        "billet.ok": "bold green",
        "billet.failed": "bold red",
    }
)

# The one shared console for the CLI process (rich resolves stdout lazily per print,
# so constructing it at import time is safe under output capture).
_shared_console = Console(theme=BILLET_THEME)


def get_console() -> Console:
    """Return the shared themed console for the CLI process."""
    return _shared_console


class _StepState(Enum):
    """Render state of one plan step in the checklist."""

    PENDING = "pending"
    RUNNING = "running"
    OK = "ok"
    FAILED = "failed"


_PLAIN_PREFIX = {
    _StepState.RUNNING: "…",
    _StepState.OK: "✓",
    _StepState.FAILED: "✗",
}

_STATIC_GLYPHS = {
    _StepState.PENDING: ("●", "billet.pending"),
    _StepState.OK: ("✓", "billet.ok"),
    _StepState.FAILED: ("✗", "billet.failed"),
}


def _line(step: PlanStep | WorkspacePlanStep, state: _StepState) -> Spinner | Text:
    """Render one checklist line for ``step`` in ``state``."""
    if state is _StepState.RUNNING:
        text = Text(step.summary, style="billet.running")
        return Spinner("dots", text=text, style="billet.running")
    glyph, style = _STATIC_GLYPHS[state]
    return Text(f"{glyph} {step.summary}", style=style)


class PlanRenderer:
    """Render a plan's steps as a live checklist while a manager applies it.

    Implements the :class:`~billet.contracts.PlanObserver` protocol. On a terminal the
    steps render through ``rich.live.Live`` — a dim ``●`` while pending, an animated
    spinner while running, a green ``✓`` / red ``✗`` once done — each followed by the
    step's summary. When stdout is not a terminal (CI, piped output) Live is skipped
    entirely and each event degrades to one plain sequential line so logs stay readable.

    Use as a context manager so the Live display is entered/exited around the apply.
    """

    def __init__(
        self,
        steps: Sequence[PlanStep | WorkspacePlanStep],
        console: Console | None = None,
    ) -> None:
        self._console = console if console is not None else get_console()
        self._steps: list[PlanStep | WorkspacePlanStep] = list(steps)
        self._states: list[_StepState] = [_StepState.PENDING] * len(self._steps)
        self._live: Live | None = None

    def __enter__(self) -> Self:
        """Start the Live checklist (terminal only) and return self as the observer."""
        if self._console.is_terminal:
            self._live = Live(self._checklist(), console=self._console, refresh_per_second=12.5)
            self._live.start()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the Live display, leaving the final checklist on screen."""
        if self._live is not None:
            self._live.stop()
            self._live = None

    # --- PlanObserver events ---------------------------------------------------------

    def step_started(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Mark the step as running (spinner / plain ``…`` line)."""
        self._transition(step, _StepState.PENDING, _StepState.RUNNING)

    def step_succeeded(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Mark the step as succeeded (green ``✓``)."""
        self._transition(step, _StepState.RUNNING, _StepState.OK)

    def step_failed(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Mark the step as failed (red ``✗``)."""
        self._transition(step, _StepState.RUNNING, _StepState.FAILED)

    # --- rendering ---------------------------------------------------------------------

    def _transition(
        self, step: PlanStep | WorkspacePlanStep, old: _StepState, new: _StepState
    ) -> None:
        for index, (candidate, state) in enumerate(zip(self._steps, self._states)):
            if state is old and candidate == step:
                self._states[index] = new
                break
        else:
            return  # unknown step: ignore rather than corrupt the checklist
        if self._live is not None:
            self._live.update(self._checklist())
        else:
            self._print_plain(step, new)

    def _checklist(self) -> Group:
        return Group(*(_line(step, state) for step, state in zip(self._steps, self._states)))

    def _print_plain(self, step: PlanStep | WorkspacePlanStep, state: _StepState) -> None:
        # Text (not markup) so summaries containing brackets render verbatim; soft_wrap
        # so one event stays one log line regardless of the (defaulted) console width.
        line = Text(f"[billet] {_PLAIN_PREFIX[state]} {step.summary}")
        self._console.print(line, soft_wrap=True)


@contextmanager
def planning_status(console: Console | None = None) -> Generator[None]:
    """Show a planning spinner for the pre-plan phase (silent when not a terminal).

    Wrap plan construction (registry reads plus the provider's ``az`` round-trips) in
    this so the operator sees activity before the plan renders. The first frame paints
    synchronously (``Console.status`` would leave it to the refresh thread, so a fast
    body could exit before anything showed). When stdout is not a terminal the body
    simply runs with no output.
    """
    active = console if console is not None else get_console()
    if not active.is_terminal:
        yield
        return
    spinner = Spinner("dots", text=Text("[billet] planning…"), style="status.spinner")
    live = Live(spinner, console=active, refresh_per_second=12.5, transient=True)
    live.start(refresh=True)
    try:
        yield
    finally:
        live.stop()
