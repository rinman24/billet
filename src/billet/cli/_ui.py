"""The billet CLI presentation layer: consoles, theme, glyphs, and shared renderables.

The only module in the codebase that imports ``rich``: all presentation stays in
``billet.cli`` (managers and access never print — ADR-0001), and the tool's look is
centralised here so restyling is a one-file change. Voice per ``brand/BRAND.md``:
terse, lowercase, present tense, no exclamation; status is a color, then a word.
"""

from collections.abc import Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
from enum import Enum
from types import TracebackType
from typing import Self

from rich.console import Console, Group
from rich.live import Live
from rich.spinner import Spinner
from rich.text import Text
from rich.theme import Theme

from billet.contracts import PlanStep, WorkspacePlanStep
from billet.shared.errors import (
    AzLoginRequired,
    BilletError,
    ConfigError,
    HostOperationError,
    ProcessError,
)

# Semantic style names mapped to the billet brand palette (BRAND §2): running is magenta,
# in-flight work and clean completion are mint, cost is amber, failure is red. Foreground
# styles only — billet never paints the terminal background. This dict is the single place
# brand colors live.
BILLET_THEME = Theme(
    {
        "running": "#C05CE0",
        "prompt": "bold #C05CE0",
        "building": "#3FD2BE",
        "done": "#3FD2BE",
        "caution": "#E6B15E",
        "error": "bold #F0706A",
        "meta": "#837390",
        "open": "#9C8BB2",
        "heading": "bold",
        # Legacy plan-renderer styles; retired with PlanRenderer once PhaseChecklist lands.
        "billet.pending": "#3FD2BE",
        "billet.running": "bold #C05CE0",
        "billet.ok": "bold #22C55E",
        "billet.failed": "bold #EF4444",
    }
)

# The 1-cell gutter glyphs every billet line leads with (the identity the old `[billet]`
# prefix carried). The spinner glyph is rich's `dots` spinner, not a constant.
GLYPH_DONE = "✓"
GLYPH_PENDING = "○"
GLYPH_STATE = "●"
GLYPH_PROMPT = "❯"
GLYPH_INFO = "·"
GLYPH_CAUTION = "!"
GLYPH_ERROR = "✗"
GLYPH_TOTAL = "↳"
BERTH_POSTED = "██"
BERTH_OPEN = "░░"


@dataclass(frozen=True, slots=True)
class UIState:
    """Global presentation flags resolved once from the root command options."""

    quiet: bool = False
    verbose: bool = False
    no_color: bool = False


def _build_console(state: UIState, *, stderr: bool = False) -> Console:
    # highlight=False: rich must not auto-recolor numbers/paths — billet owns every accent.
    # no_color=None lets rich honor the NO_COLOR env var; --no-color forces it off.
    return Console(
        theme=BILLET_THEME,
        highlight=False,
        stderr=stderr,
        no_color=True if state.no_color else None,
    )


@dataclass
class _UIRuntime:
    """The process-wide presentation state and its consoles (rebuilt by ``configure``)."""

    state: UIState
    console: Console
    error_console: Console


# The one shared runtime for the CLI process (rich resolves stdout/stderr lazily per
# print, so constructing the consoles at import time is safe under output capture).
_runtime = _UIRuntime(
    state=UIState(),
    console=_build_console(UIState()),
    error_console=_build_console(UIState(), stderr=True),
)


def configure(state: UIState) -> None:
    """Apply the root-command flags, rebuilding both consoles so ``--no-color`` sticks."""
    _runtime.state = state
    _runtime.console = _build_console(state)
    _runtime.error_console = _build_console(state, stderr=True)


def get_state() -> UIState:
    """Return the presentation flags for the current invocation."""
    return _runtime.state


def get_console() -> Console:
    """Return the shared themed stdout console for the CLI process."""
    return _runtime.console


def get_error_console() -> Console:
    """Return the shared themed stderr console (the error view prints here)."""
    return _runtime.error_console


def animate() -> bool:
    """Return whether live displays may animate: a real tty and not ``--quiet``.

    The golden rule — every spinner, ``Live``, and progress bar must be guarded by this
    so piped and CI output stays plain, aligned, and greppable.
    """
    return _runtime.console.is_terminal and not _runtime.state.quiet


# --- error view ------------------------------------------------------------------------

_STDERR_TAIL_LINES = 5


def render_error(exc: BilletError, console: Console | None = None) -> None:
    """Render a :class:`BilletError` as ``✗ headline`` + a remediation block on stderr.

    No traceback ever reaches the operator: each subtype maps to a one-line headline and
    a short remediation, with the exact next command left in ink so it is copy-pasteable.
    """
    err = console if console is not None else get_error_console()
    headline, remediation = _error_view(exc)
    err.print(Text(f"{GLYPH_ERROR} {headline}", style="error"), soft_wrap=True)
    if remediation is not None:
        err.print()
        err.print(remediation, soft_wrap=True)


def _error_view(exc: BilletError) -> tuple[str, Group | None]:
    """Map an error subtype to its headline and optional remediation renderable."""
    if isinstance(exc, AzLoginRequired):
        return "az login required", Group(
            Text("  billet can't reach azure.", style="meta"),
            Text("  sign in, then retry:", style="meta"),
            Text("    az login"),
        )
    if isinstance(exc, ProcessError):
        return _process_error_view(exc)
    if isinstance(exc, ConfigError):
        lines = [Text(f"  {line}", style="meta") for line in str(exc).splitlines()]
        lines.append(Text("  edit it, then retry"))
        return "config error", Group(*lines)
    if isinstance(exc, HostOperationError):
        # The message is verbatim manager copy that already suggests the fix; the first
        # sentence is the headline, any remainder becomes the remediation body.
        headline, _, rest = str(exc).partition(". ")
        if not rest:
            return str(exc), None
        return headline, Group(Text(f"  {rest}", style="meta"))
    return str(exc), None


def _process_error_view(exc: ProcessError) -> tuple[str, Group]:
    """Build the ``{op} failed · exit {n}`` view with a stderr tail (full under -v)."""
    op = exc.argv[0] if exc.argv else "command"
    headline = f"{op} failed · exit {exc.returncode}"
    tail = exc.stderr.rstrip().splitlines()
    lines: list[Text] = []
    if get_state().verbose:
        lines.append(Text(f"  {' '.join(exc.argv)}", style="meta"))
    else:
        tail = tail[-_STDERR_TAIL_LINES:]
    if any(line.strip() for line in tail):
        lines.append(Text("  ↓ last output", style="dim"))
        lines.extend(Text(f"    {line}", style="dim") for line in tail)
    if not get_state().verbose:
        lines.append(Text("  retry with --verbose for the full command output", style="meta"))
    return headline, Group(*lines)


# --- live plan progress (retired for PhaseChecklist in the live-checklist phase) --------


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
