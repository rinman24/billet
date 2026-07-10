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
import re
from types import TracebackType
from typing import Self

from rich.console import Console, Group
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

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


# --- one-line helpers --------------------------------------------------------------


def info(message: str, console: Console | None = None) -> None:
    """Print an info line: the muted ``·`` gutter plus a muted message."""
    out = console if console is not None else get_console()
    out.print(Text(f"{GLYPH_INFO} {message}", style="meta"), soft_wrap=True)


def success(message: str, detail: str | None = None, console: Console | None = None) -> None:
    """Print an outcome line: mint ``✓``, message in ink, optional ``· detail`` muted."""
    out = console if console is not None else get_console()
    line = Text.assemble((GLYPH_DONE, "done"), " ", message)
    if detail:
        line.append(f" {GLYPH_INFO} {detail}", style="meta")
    out.print(line, soft_wrap=True)


def caution(message: str, console: Console | None = None) -> None:
    """Print a caution line: amber ``!`` gutter plus the message in ink."""
    out = console if console is not None else get_console()
    out.print(Text.assemble((GLYPH_CAUTION, "caution"), " ", message), soft_wrap=True)


def next_hint(*commands: str, console: Console | None = None) -> None:
    """Print the ``· next`` line — the follow-up commands in ink, connectors muted."""
    out = console if console is not None else get_console()
    line = Text(f"{GLYPH_INFO} next  ", style="meta")
    for index, command in enumerate(commands):
        if index:
            line.append("  then  ", style="meta")
        line.append(command)
    out.print(line, soft_wrap=True)


def hint(label: str, command: str, console: Console | None = None) -> None:
    """Print ``· {label} → {command}`` with the command in ink (copy-pasteable)."""
    out = console if console is not None else get_console()
    line = Text(f"{GLYPH_INFO} {label} → ", style="meta")
    line.append(command)
    out.print(line, soft_wrap=True)


# --- banner + bare-billet command surface --------------------------------------------

_TAGLINE = "a berth for every repo"


def banner(version: str, console: Console | None = None) -> None:
    """Print the berth-rack banner (bare ``billet``); one plain line off-tty / --quiet.

    Posted cells run magenta, mint, magenta top-to-bottom; open cells stay open. The
    rack never fills — billet always leaves berths open. The wordmark stays ink.
    """
    out = console if console is not None else get_console()
    if not out.is_terminal or get_state().quiet:
        out.print(f"billet {version}")
        return
    out.print(
        Text.assemble(
            (BERTH_POSTED, "running"), " ", (BERTH_OPEN, "open"), "   ", f"billet {version}"
        )
    )
    out.print(
        Text.assemble(
            (BERTH_OPEN, "open"), " ", (BERTH_POSTED, "building"), "   ", (_TAGLINE, "meta")
        )
    )
    out.print(Text.assemble((BERTH_POSTED, "running"), " ", (BERTH_OPEN, "open")))


_COMMAND_SURFACE: tuple[tuple[str, str, tuple[tuple[str, str], ...]], ...] = (
    (
        "host",
        "vm lifecycle",
        (
            ("host up", "create or resume a host"),
            ("host stop", "deallocate — stops compute billing"),
            ("host pin-ip", "re-pin inbound ssh to your /32"),
            ("host specs", "live cpu / memory / disk / container usage"),
        ),
    ),
    (
        "workspace",
        "devcontainers on a host",
        (
            ("add <key>", "validate a workspace block"),
            ("start <key>", "clone · build · bootstrap on a host"),
            ("stop <key>", "stop the compose stack"),
            ("connect <key>", "ssh in and attach to tmux"),
            ("ls", "list berths — running · stopped · open"),
            ("ssh-config", "write ~/.ssh/config.d/billet.conf"),
            ("rm <key>", "how to deregister"),
        ),
    ),
)


def command_surface(console: Console | None = None) -> None:
    """Print the grouped command listing shown on bare ``billet``."""
    out = console if console is not None else get_console()
    out.print()
    out.print(Text.assemble(("usage", "meta"), "  billet <command> [args]"))
    out.print()
    for group, tagline, commands in _COMMAND_SURFACE:
        out.print(Text.assemble((group, "heading"), (f" {GLYPH_INFO} {tagline}", "meta")))
        for command, description in commands:
            out.print(Text.assemble(f"  {command:<16}", (description, "meta")))
    out.print()
    out.print(Text("run billet <command> --help for details.", style="meta"))


# --- plan view + nothing-to-do (§6.4) -------------------------------------------------

# The dry-run plan keeps each step summary verbatim, minus the all-caps billable shout
# (the right-aligned `! billable` tag carries it) and lowercased per the brand voice.
_BILLABLE_SHOUT = re.compile(r",?\s*BILLABLE")


def _plan_row_summary(step: PlanStep | WorkspacePlanStep) -> str:
    return _BILLABLE_SHOUT.sub("", step.summary).lower()


def _host_plan_mode(steps: Sequence[PlanStep]) -> str:
    kinds: set[StepKind] = {step.kind for step in steps}
    if StepKind.CREATE in kinds:
        return "cold create"
    if StepKind.START in kinds:
        return "resume"
    if StepKind.DEALLOCATE in kinds:
        return "deallocate"
    if kinds == {StepKind.PIN_INBOUND}:
        return "pin"
    return "adopt"


def _workspace_plan_mode(steps: Sequence[WorkspacePlanStep]) -> str:
    kinds: set[WorkspaceStepKind] = {step.kind for step in steps}
    return "stop" if WorkspaceStepKind.COMPOSE_STOP in kinds else "start"


def _plan_header(noun: str, key: str, mode: str) -> Text:
    return Text.assemble(
        ("plan", "heading"),
        (f" {GLYPH_INFO} ", "meta"),
        f"{noun} {key}",
        (f" {GLYPH_INFO} ", "meta"),
        (mode, "running"),
    )


def nothing_to_do(key: str, already: str | None = None, console: Console | None = None) -> None:
    """Print the empty-plan info line, naming the state when the caller knows it."""
    if already:
        info(f"nothing to do — {key} already {already}", console)
    else:
        info("nothing to do", console)


def render_host_plan(
    plan: Plan, already: str | None = None, console: Console | None = None
) -> None:
    """Render a host plan: header, numbered rows, billable rows tagged ``! billable``."""
    out = console if console is not None else get_console()
    if plan.is_empty:
        nothing_to_do(plan.host_key, already, out)
        return
    out.print(_plan_header("host", plan.host_key, _host_plan_mode(plan.steps)))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="meta")
    grid.add_column()
    grid.add_column(justify="right")
    for number, step in enumerate(plan.steps, 1):
        tag = Text(f"{GLYPH_CAUTION} billable", style="caution") if step.billable else Text("")
        grid.add_row(str(number), _plan_row_summary(step), tag)
    out.print(Padding(grid, (0, 0, 0, 2)))


def render_workspace_plan(plan: WorkspacePlan, console: Console | None = None) -> None:
    """Render a workspace plan: header plus numbered rows (workspace steps never bill)."""
    out = console if console is not None else get_console()
    if plan.is_empty:
        nothing_to_do(plan.workspace_key, console=out)
        return
    out.print(_plan_header("workspace", plan.workspace_key, _workspace_plan_mode(plan.steps)))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="meta")
    grid.add_column()
    for number, step in enumerate(plan.steps, 1):
        grid.add_row(str(number), _plan_row_summary(step))
    out.print(Padding(grid, (0, 0, 0, 2)))


# --- ls view (§6.5) --------------------------------------------------------------------


@dataclass(frozen=True, slots=True)
class LsWorkspaceRow:
    """One workspace row of the ls view (a pure view-model built by the command)."""

    key: str
    state: str  # "running" | "stopped" | "invalid" | "unreachable"
    alias: str
    port: int


@dataclass(frozen=True, slots=True)
class LsHostGroup:
    """One host section of the ls view: the rack and the berths posted into it."""

    key: str
    vm_size: str
    manages_workspaces: bool
    rows: tuple[LsWorkspaceRow, ...]


_LS_STATE_GLYPHS: dict[str, tuple[str, str]] = {
    "running": (GLYPH_STATE, "running"),
    "stopped": (GLYPH_PENDING, "meta"),
    "invalid": (GLYPH_STATE, "error"),
    "unreachable": (GLYPH_PENDING, "caution"),
}

# The brand rack has six berths; the glyph is a visual metaphor, not a quota, so the
# total simply grows past six if an operator posts more workspaces than the mark holds.
_RACK_BERTHS = 6


def _rack_cells(posted: int) -> Text:
    total: int = max(_RACK_BERTHS, posted)
    cells = Text()
    for berth in range(total):
        if berth < posted:
            cells.append("█", style="running" if berth % 2 == 0 else "building")
        else:
            cells.append("░", style="open")
    return cells


def _ls_host_line(group: LsHostGroup) -> Text:
    line = Text.assemble((group.key, "heading"), "   ", (group.vm_size.lower(), "meta"))
    if not group.manages_workspaces:
        line.append(f" {GLYPH_INFO} manages no workspaces", style="dim")
        return line
    posted: int = len(group.rows)
    line.append(f" {GLYPH_INFO} ", style="meta")
    line.append_text(_rack_cells(posted))
    line.append(f"  {posted} / {max(_RACK_BERTHS, posted)}", style="meta")
    return line


def _ls_unreachable_hint(host_key: str) -> Text:
    line = Text(f"{GLYPH_INFO} host {host_key} is unreachable — bring it up with ", style="meta")
    line.append(f"billet host up --host {host_key}")
    return line


def render_ls(groups: Sequence[LsHostGroup], console: Console | None = None) -> None:
    """Render ``billet ls``: grouped racks on a tty, a fixed-column table when piped."""
    out = console if console is not None else get_console()
    if not out.is_terminal:
        _render_ls_plain(groups, out)
        return
    for index, group in enumerate(groups):
        if index:
            out.print()
        out.print(_ls_host_line(group))
        if group.rows:
            grid = Table.grid(padding=(0, 3))
            grid.add_column(width=1)
            grid.add_column()
            grid.add_column()
            grid.add_column()
            grid.add_column()
            for row in group.rows:
                glyph, style = _LS_STATE_GLYPHS[row.state]
                grid.add_row(
                    Text(glyph, style=style),
                    row.key,
                    Text(row.state, style=style),
                    Text(row.alias, style="meta"),
                    Text(f":{row.port}", style="meta"),
                )
            out.print(Padding(grid, (0, 0, 0, 2)))
        if any(row.state == "unreachable" for row in group.rows):
            out.print(_ls_unreachable_hint(group.key))


def _render_ls_plain(groups: Sequence[LsHostGroup], out: Console) -> None:
    """Render the piped ls: an uppercase header plus one aligned, greppable record per line."""
    header: tuple[str, str, str, str] = ("HOST", "WORKSPACE", "STATE", "PORT")
    records: list[tuple[str, str, str, str]] = [
        (group.key, row.key, row.state, str(row.port)) for group in groups for row in group.rows
    ]
    widths: list[int] = [
        max(len(header[column]), *(len(record[column]) for record in records))
        if records
        else len(header[column])
        for column in range(len(header))
    ]
    for record in (header, *records):
        line: str = "  ".join(value.ljust(width) for value, width in zip(record, widths))
        out.print(Text(line.rstrip()), soft_wrap=True)
    for group in groups:
        if any(row.state == "unreachable" for row in group.rows):
            out.print(_ls_unreachable_hint(group.key), soft_wrap=True)


# --- empty state (§6.7) ----------------------------------------------------------------


def empty_state(lines: Sequence[str], console: Console | None = None) -> None:
    """Render an all-open rack beside guidance lines (plain lines off-tty / --quiet)."""
    out = console if console is not None else get_console()
    if not out.is_terminal or get_state().quiet:
        for line in lines:
            out.print(Text(line), soft_wrap=True)
        return
    rack: str = f"{BERTH_OPEN} {BERTH_OPEN}"
    for index in range(max(3, len(lines))):
        text: str = lines[index] if index < len(lines) else ""
        style: str = "" if index == 0 else "meta"
        out.print(Text.assemble((rack, "open"), "   ", (text, style)))


# --- titled blocks ----------------------------------------------------------------------


def block_panel(
    title: str,
    body: str,
    *,
    border_style: str = "meta",
    console: Console | None = None,
) -> None:
    """Print a titled panel around a plain mono block (title + bare body when piped).

    The body stays unstyled so it is copy-pasteable; off-tty the panel border is dropped
    entirely so the block can be piped straight into a file.
    """
    out = console if console is not None else get_console()
    if not out.is_terminal:
        out.print(Text(title), soft_wrap=True)
        out.print(Text(body.rstrip("\n")), soft_wrap=True)
        return
    out.print(
        Panel(
            Text(body.rstrip("\n")),
            title=title,
            title_align="left",
            border_style=border_style,
            expand=False,
            padding=(0, 2),
        )
    )


def titled_steps(
    title: str,
    note: str,
    steps: Sequence[tuple[str, str]],
    console: Console | None = None,
) -> None:
    """Print a titled note: heading, a muted note line, then numbered steps.

    Each step is ``(label, command)``; the command stays in ink (copy-pasteable) and may
    be empty for a step that is prose only.
    """
    out = console if console is not None else get_console()
    out.print(Text(title, style="heading"))
    out.print(Text(f"  {note}", style="meta"))
    for number, (label, command) in enumerate(steps, 1):
        line = Text(f"  {number}  ", style="meta")
        line.append(label, style="meta")
        if command:
            line.append("   ")
            line.append(command)
        out.print(line, soft_wrap=True)


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
    spinner = Spinner("dots", text=Text("planning…", style="meta"), style="building")
    live = Live(spinner, console=active, refresh_per_second=12.5, transient=True)
    live.start(refresh=True)
    try:
        yield
    finally:
        live.stop()
