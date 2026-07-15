"""The billet CLI presentation layer: consoles, theme, glyphs, and shared renderables.

The only module in the codebase that imports ``rich``: all presentation stays in
``billet.cli`` (managers and access never print — ADR-0001), and the tool's look is
centralised here so restyling is a one-file change. Voice per ``brand/BRAND.md``:
terse, lowercase, present tense, no exclamation; status is a color, then a word.
"""

from collections.abc import Callable, Generator, Sequence
from contextlib import contextmanager
from dataclasses import dataclass
import json
import re
import time
from types import TracebackType
from typing import Self

from rich.console import Console, Group, RenderableType
from rich.live import Live
from rich.padding import Padding
from rich.panel import Panel
from rich.progress_bar import ProgressBar
from rich.spinner import Spinner
from rich.table import Table
from rich.text import Text
from rich.theme import Theme

from billet.contracts import (
    HostSpec,
    Plan,
    PlanStep,
    StepKind,
    WorkspacePlan,
    WorkspacePlanStep,
    WorkspaceSpec,
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
    }
)


@dataclass(frozen=True, slots=True)
class GlyphSet:
    """The gutter glyph vocabulary every billet line leads with (§3).

    The glyphs carry the identity the old ``[billet]`` prefix did. ``spinner`` names a
    rich spinner. The ascii set is the §3 fallback table for terminals that cannot
    render box/braille characters (or ``--ascii``).
    """

    done: str
    pending: str
    state: str
    prompt: str
    info: str
    caution: str
    error: str
    total: str
    header: str
    berth_posted: str
    berth_open: str
    cell_posted: str
    cell_open: str
    spinner: str


_UNICODE_GLYPHS = GlyphSet(
    done="✓",
    pending="○",
    state="●",
    prompt="❯",
    info="·",
    caution="!",
    error="✗",
    total="↳",
    header="»",
    berth_posted="██",
    berth_open="░░",
    cell_posted="█",
    cell_open="░",
    spinner="dots",
)

_ASCII_GLYPHS = GlyphSet(
    done="[ok]",
    pending="[ ]",
    state="*",
    prompt=">",
    info="-",
    caution="[!]",
    error="[x]",
    total=">",
    header=">>",
    berth_posted="##",
    berth_open="..",
    cell_posted="#",
    cell_open=".",
    spinner="simpleDots",
)


@dataclass(frozen=True, slots=True)
class UIState:
    """Global presentation flags resolved once from the root command options."""

    quiet: bool = False
    verbose: bool = False
    no_color: bool = False
    ascii_only: bool = False


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


def glyphs() -> GlyphSet:
    """Return the active glyph set: unicode, or the §3 ascii fallback.

    Falls back on ``--ascii``, or when the shared console's output encoding cannot
    carry the box/braille glyphs (e.g. a C-locale pipe).
    """
    if _runtime.state.ascii_only:
        return _ASCII_GLYPHS
    if "utf" not in (_runtime.console.encoding or "").lower():
        return _ASCII_GLYPHS
    return _UNICODE_GLYPHS


# --- error view ------------------------------------------------------------------------

_STDERR_TAIL_LINES = 5


def render_error(exc: BilletError, console: Console | None = None) -> None:
    """Render a :class:`BilletError` as ``✗ headline`` + a remediation block on stderr.

    No traceback ever reaches the operator: each subtype maps to a one-line headline and
    a short remediation, with the exact next command left in ink so it is copy-pasteable.
    """
    err = console if console is not None else get_error_console()
    headline, remediation = _error_view(exc)
    err.print(Text(f"{glyphs().error} {headline}", style="error"), soft_wrap=True)
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
    out.print(Text(f"{glyphs().info} {message}", style="meta"), soft_wrap=True)


def success(message: str, detail: str | None = None, console: Console | None = None) -> None:
    """Print an outcome line: mint ``✓``, message in ink, optional ``· detail`` muted."""
    out = console if console is not None else get_console()
    line = Text.assemble((glyphs().done, "done"), " ", message)
    if detail:
        line.append(f" {glyphs().info} {detail}", style="meta")
    out.print(line, soft_wrap=True)


def caution(message: str, console: Console | None = None) -> None:
    """Print a caution line: amber ``!`` gutter plus the message in ink."""
    out = console if console is not None else get_console()
    out.print(Text.assemble((glyphs().caution, "caution"), " ", message), soft_wrap=True)


def next_hint(*commands: str, console: Console | None = None) -> None:
    """Print the ``· next`` line — the follow-up commands in ink, connectors muted.

    An info line, so ``--quiet`` suppresses it (outcome lines still print).
    """
    if get_state().quiet:
        return
    out = console if console is not None else get_console()
    line = Text()  # no base style: appended commands must stay ink
    line.append(f"{glyphs().info} next  ", style="meta")
    for index, command in enumerate(commands):
        if index:
            line.append("  then  ", style="meta")
        line.append(command)
    out.print(line, soft_wrap=True)


def hint(label: str, command: str, console: Console | None = None) -> None:
    """Print ``· {label} → {command}`` with the command in ink (copy-pasteable).

    An info line, so ``--quiet`` suppresses it (outcome lines still print).
    """
    if get_state().quiet:
        return
    out = console if console is not None else get_console()
    line = Text()  # no base style: the appended command must stay ink
    line.append(f"{glyphs().info} {label} → ", style="meta")
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
    g = glyphs()
    out.print(
        Text.assemble(
            (g.berth_posted, "running"), " ", (g.berth_open, "open"), "   ", f"billet {version}"
        )
    )
    out.print(
        Text.assemble(
            (g.berth_open, "open"), " ", (g.berth_posted, "building"), "   ", (_TAGLINE, "meta")
        )
    )
    out.print(Text.assemble((g.berth_posted, "running"), " ", (g.berth_open, "open")))


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
        out.print(Text.assemble((group, "heading"), (f" {glyphs().info} {tagline}", "meta")))
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


def host_plan_mode(steps: Sequence[PlanStep]) -> str:
    """Name the shape of a host plan from its step kinds (plan header, checklist title)."""
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
        (f" {glyphs().info} ", "meta"),
        f"{noun} {key}",
        (f" {glyphs().info} ", "meta"),
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
    out.print(_plan_header("host", plan.host_key, host_plan_mode(plan.steps)))
    grid = Table.grid(padding=(0, 2))
    grid.add_column(justify="right", style="meta")
    grid.add_column()
    grid.add_column(justify="right")
    for number, step in enumerate(plan.steps, 1):
        tag = Text(f"{glyphs().caution} billable", style="caution") if step.billable else Text("")
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
    """One host section of the ls view: the rack and the berths posted into it.

    ``vm_size`` is None for an adopted host whose table carries no provisioning keys;
    the header renders a placeholder dash in its place.
    """

    key: str
    vm_size: str | None
    manages_workspaces: bool
    rows: tuple[LsWorkspaceRow, ...]


def _ls_state_glyph(state: str) -> tuple[str, str]:
    g = glyphs()
    table: dict[str, tuple[str, str]] = {
        "running": (g.state, "running"),
        "stopped": (g.pending, "meta"),
        "invalid": (g.state, "error"),
        "unreachable": (g.pending, "caution"),
    }
    return table[state]


def no_size_placeholder() -> str:
    """Return the stand-in for a host with no declared ``vm_size`` (adopted, never provisioned)."""
    return "-" if glyphs() is _ASCII_GLYPHS else "—"


# The brand rack has six berths; the glyph is a visual metaphor, not a quota, so the
# total simply grows past six if an operator posts more workspaces than the mark holds.
_RACK_BERTHS = 6


def _rack_cells(posted: int) -> Text:
    g = glyphs()
    total: int = max(_RACK_BERTHS, posted)
    cells = Text()
    for berth in range(total):
        if berth < posted:
            cells.append(g.cell_posted, style="running" if berth % 2 == 0 else "building")
        else:
            cells.append(g.cell_open, style="open")
    return cells


def _ls_host_line(group: LsHostGroup) -> Text:
    size = group.vm_size.lower() if group.vm_size else no_size_placeholder()
    line = Text.assemble((group.key, "heading"), "   ", (size, "meta"))
    if not group.manages_workspaces:
        line.append(f" {glyphs().info} manages no workspaces", style="dim")
        return line
    posted: int = len(group.rows)
    line.append(f" {glyphs().info} ", style="meta")
    line.append_text(_rack_cells(posted))
    line.append(f"  {posted} / {max(_RACK_BERTHS, posted)}", style="meta")
    return line


def _ls_unreachable_hint(host_key: str) -> Text:
    line = Text()  # no base style: the appended command must stay ink
    line.append(f"{glyphs().info} host {host_key} is unreachable — bring it up with ", style="meta")
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
                glyph, style = _ls_state_glyph(row.state)
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


def render_ls_json(groups: Sequence[LsHostGroup], console: Console | None = None) -> None:
    """Emit ls as one machine-readable JSON array — no styling, one record per workspace."""
    records: list[dict[str, str | int]] = [
        {
            "host": group.key,
            "key": row.key,
            "state": row.state,
            "alias": row.alias,
            "port": row.port,
        }
        for group in groups
        for row in group.rows
    ]
    out = console if console is not None else get_console()
    out.print(Text(json.dumps(records)), soft_wrap=True)


# --- empty state (§6.7) ----------------------------------------------------------------


def empty_state(lines: Sequence[str], console: Console | None = None) -> None:
    """Render an all-open rack beside guidance lines (plain lines off-tty / --quiet)."""
    out = console if console is not None else get_console()
    if not out.is_terminal or get_state().quiet:
        for line in lines:
            out.print(Text(line), soft_wrap=True)
        return
    rack: str = f"{glyphs().berth_open} {glyphs().berth_open}"
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
        line = Text()  # no base style: the appended command must stay ink
        line.append(f"  {number}  {label}", style="meta")
        if command:
            line.append(f"   {command}")
        out.print(line, soft_wrap=True)


# --- the live phase checklist (§6.2) ----------------------------------------------------


def _fmt_mss(seconds: float) -> str:
    """Format elapsed seconds as ``M:SS`` (``0:47``, ``1:44``)."""
    minutes, secs = divmod(int(seconds), 60)
    return f"{minutes}:{secs:02d}"


def _repo_short(repo_url: str) -> str:
    """Reduce a repo url to ``owner/name`` (works for scp-like and https forms)."""
    tail: str = repo_url.rstrip("/").removesuffix(".git").replace(":", "/")
    return "/".join(tail.split("/")[-2:])


@dataclass
class Phase:
    """One row of the live checklist: a plan step with render state and timing."""

    key: str
    label: str
    group: str = ""
    bar: bool = False
    state: str = "pending"  # pending | active | done | failed
    t0: float | None = None
    t1: float | None = None
    progress: float | None = None
    last_line: str = ""

    def elapsed(self) -> float:
        """Return the phase's elapsed seconds (live while active, frozen once ended)."""
        if self.t0 is None:
            return 0.0
        end: float = self.t1 if self.t1 is not None else time.monotonic()
        return end - self.t0


def phase_key(step: PlanStep | WorkspacePlanStep) -> str:
    """Return the checklist key a plan step maps to (host and workspace kinds differ)."""
    group = "host" if isinstance(step, PlanStep) else "workspace"
    return f"{group}:{step.kind.value}"


def _host_phase_label(kind: StepKind, spec: HostSpec) -> str:
    labels: dict[StepKind, str] = {
        StepKind.CREATE: f"create vm {spec.vm_name}",
        StepKind.ENSURE_TAGS: f"adopt vm {spec.vm_name}",
        StepKind.PIN_INBOUND: "pin inbound ssh",
        StepKind.START: f"start vm {spec.vm_name}",
        StepKind.DEALLOCATE: f"deallocate vm {spec.vm_name}",
        StepKind.WAIT_REACHABLE: "wait for ssh",
        StepKind.ENSURE_SUPPLY_CHAIN: "install docker",
    }
    return labels[kind]


def _workspace_phase_label(kind: WorkspaceStepKind, spec: WorkspaceSpec) -> str:
    labels: dict[WorkspaceStepKind, str] = {
        WorkspaceStepKind.ENSURE_SOURCE: f"clone / fast-forward {_repo_short(spec.repo_url)}",
        WorkspaceStepKind.COMPOSE_UP: "docker compose up · build",
        WorkspaceStepKind.POST_CREATE: "postCreate",
        WorkspaceStepKind.PERSONAL_BOOTSTRAP: "personal bootstrap",
        WorkspaceStepKind.VERIFY: "verify",
        WorkspaceStepKind.COMPOSE_STOP: "stop compose stack",
    }
    return labels[kind]


def host_phases(plan: Plan, spec: HostSpec) -> list[Phase]:
    """Build checklist phases from a host plan (compact present-tense labels)."""
    return [
        Phase(key=phase_key(step), label=_host_phase_label(step.kind, spec), group="host")
        for step in plan.steps
    ]


def workspace_phases(plan: WorkspacePlan, spec: WorkspaceSpec) -> list[Phase]:
    """Build checklist phases from a workspace plan (compose-up carries the bar)."""
    return [
        Phase(
            key=phase_key(step),
            label=_workspace_phase_label(step.kind, spec),
            group="workspace",
            bar=step.kind is WorkspaceStepKind.COMPOSE_UP,
        )
        for step in plan.steps
    ]


def _phase_gutter(state: str) -> tuple[str, str]:
    g = glyphs()
    table: dict[str, tuple[str, str]] = {
        "pending": (g.pending, "open"),
        "done": (g.done, "done"),
        "failed": (g.error, "error"),
    }
    return table[state]


_BAR_CELLS = 24

# The checklist key of the streaming compose-up phase (the log-tail target).
COMPOSE_UP_KEY = f"workspace:{WorkspaceStepKind.COMPOSE_UP.value}"

# BuildKit step markers: `#9 [build 5/9] COPY …` (named stage) or `#5 [2/7] RUN …` (base).
_BUILDKIT_STEP = re.compile(r"#\d+ \[(?:[^\[\]]* )?(\d+)/(\d+)\]")


def buildkit_fraction(line: str) -> float | None:
    """Parse a BuildKit step marker into a 0..1 fraction (None when ``line`` is not one).

    Best-effort by design: a match drives the determinate bar, anything else leaves the
    pulse bar in place.
    """
    match = _BUILDKIT_STEP.search(line)
    if match is None:
        return None
    step, total = int(match.group(1)), int(match.group(2))
    if total <= 0 or step > total:
        return None
    return step / total


class PhaseChecklist:
    """A live, in-place checklist that ticks phases as a manager applies a plan.

    Implements the :class:`~billet.contracts.PlanObserver` protocol, so the CLI passes
    it straight into ``apply``. On a tty (and not ``--quiet``) the rows render through
    ``rich.live.Live``: ``○`` pending → mint dots spinner with a live elapsed while
    active → ``✓`` with the frozen elapsed once done (``✗`` on failure); a ``bar``
    phase shows a pulse bar (and, when fed, a log tail) while active. Piped / CI output
    degrades to one plain completion line per phase; under ``--quiet`` the checklist is
    silent and only the caller's outcome line prints.

    Use as a context manager so the Live display wraps the apply calls.
    """

    def __init__(self, phases: Sequence[Phase], title: str, console: Console | None = None) -> None:
        self._console = console if console is not None else get_console()
        self._phases: list[Phase] = list(phases)
        self._by_key: dict[str, Phase] = {phase.key: phase for phase in self._phases}
        self._title = title
        self._quiet: bool = get_state().quiet
        self._verbose: bool = get_state().verbose
        # -v swaps the animated checklist for plain per-phase headers + raw output (§7.1).
        self._animate: bool = self._console.is_terminal and not self._quiet and not self._verbose
        self._spinner = Spinner(glyphs().spinner, style="building")
        self._live: Live | None = None

    def __enter__(self) -> Self:
        """Start the Live display (animated path only) and return self as the observer."""
        if self._animate and self._phases:
            self._live = Live(self, console=self._console, refresh_per_second=12.5, transient=False)
            self._live.__enter__()
        return self

    def __exit__(
        self,
        exc_type: type[BaseException] | None,
        exc_value: BaseException | None,
        traceback: TracebackType | None,
    ) -> None:
        """Stop the Live display, leaving the finished checklist on screen."""
        if self._live is not None:
            self._live.__exit__(exc_type, exc_value, traceback)
            self._live = None

    # --- PlanObserver events -----------------------------------------------------------

    def step_started(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Mark the step's phase active and start its clock."""
        phase = self._by_key.get(phase_key(step))
        if phase is None:
            return  # unknown step: ignore rather than corrupt the checklist
        phase.state = "active"
        phase.t0 = time.monotonic()
        if self._verbose and not self._quiet:
            self._console.print(
                Text(f"{glyphs().header} {phase.label}", style="meta"), soft_wrap=True
            )

    def step_succeeded(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Mark the step's phase done, freezing its elapsed."""
        self._finish(step, "done")

    def step_failed(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Mark the step's phase failed; the checklist stops with the ✗ row visible."""
        self._finish(step, "failed")

    def _finish(self, step: PlanStep | WorkspacePlanStep, state: str) -> None:
        phase = self._by_key.get(phase_key(step))
        if phase is None:
            return
        phase.state = state
        phase.t1 = time.monotonic()
        if not self._quiet and not self._animate:
            word = "failed" if state == "failed" else "ok"
            style = "error" if state == "failed" else ""
            self._console.print(Text(f"  {phase.label} … {word}", style=style), soft_wrap=True)

    # --- phase-4 seams (fed by the compose-up log stream) --------------------------------

    def set_progress(self, key: str, fraction: float | None) -> None:
        """Set a real 0..1 fraction for a bar phase (None reverts to the pulse bar)."""
        phase = self._by_key.get(key)
        if phase is not None:
            phase.progress = fraction

    def log(self, key: str, line: str) -> None:
        """Show ``line`` as the live log tail under the phase's bar (animated only)."""
        phase = self._by_key.get(key)
        if phase is not None and line.strip():
            phase.last_line = line.strip()

    # --- rendering -----------------------------------------------------------------------

    def total_elapsed(self) -> str:
        """Return the ``M:SS`` sum of elapsed time across all phases."""
        return _fmt_mss(sum(phase.elapsed() for phase in self._phases))

    def _gutter(self, phase: Phase) -> Spinner | Text:
        if phase.state == "active":
            return self._spinner
        glyph, style = _phase_gutter(phase.state)
        return Text(glyph, style=style)

    def _bar(self, phase: Phase) -> ProgressBar | Text:
        g = glyphs()
        if g is _ASCII_GLYPHS:
            # Hand-rolled cells: rich's bar glyphs are box-drawing characters.
            filled: int = round((phase.progress or 0.0) * _BAR_CELLS)
            bar = Text(g.cell_posted * filled, style="building")
            bar.append(g.cell_open * (_BAR_CELLS - filled), style="meta")
            return bar
        if phase.progress is not None:
            return ProgressBar(
                total=1.0, completed=phase.progress, width=_BAR_CELLS, complete_style="building"
            )
        return ProgressBar(pulse=True, width=_BAR_CELLS, pulse_style="building")

    @staticmethod
    def _row_grid() -> Table:
        # Gutter width tracks the glyph set: 1 cell for unicode, wider for ascii tags.
        g = glyphs()
        gutter: int = max(len(g.done), len(g.pending), len(g.error)) + 2
        grid = Table.grid(expand=True, padding=(0, 1))
        grid.add_column(width=gutter, justify="right")
        grid.add_column(ratio=1, no_wrap=True, overflow="ellipsis")
        grid.add_column(width=6, justify="right")
        return grid

    def _rows(self, phases: Sequence[Phase]) -> Group:
        grid = self._row_grid()
        renderables: list[Table | Padding] = [grid]
        for phase in phases:
            label_style = "dim" if phase.state == "pending" else ""
            elapsed = Text(_fmt_mss(phase.elapsed()) if phase.t0 else "", style="meta")
            grid.add_row(self._gutter(phase), Text(phase.label, style=label_style), elapsed)
            if phase.bar and phase.state == "active":
                renderables.append(Padding(self._bar(phase), (0, 0, 0, 6)))
                if phase.last_line:
                    tail = Text(phase.last_line, style="dim", overflow="ellipsis", no_wrap=True)
                    renderables.append(Padding(tail, (0, 0, 0, 6)))
                grid = self._row_grid()
                renderables.append(grid)
        return Group(*renderables)

    def compose_tail(self) -> Callable[[str], None]:
        """Build the line sink for the compose-up stream: log tail + BuildKit fraction.

        Safe to call from the runner's reader threads — each event is a plain attribute
        write; the Live refresh thread picks it up on its next frame.
        """

        def feed(line: str) -> None:
            if self._verbose and not self._quiet:
                # -v: the raw underlying output, interleaved below the phase header.
                self._console.print(Text(f"  {line}", style="dim"), soft_wrap=True)
                return
            self.log(COMPOSE_UP_KEY, line)
            fraction = buildkit_fraction(line)
            if fraction is not None:
                self.set_progress(COMPOSE_UP_KEY, fraction)

        return feed

    def __rich__(self) -> Group:
        """Render the checklist frame: title + running total, group rules, phase rows."""
        header = Table.grid(expand=True)
        header.add_column()
        header.add_column(justify="right")
        header.add_row(
            Text(self._title),
            Text(f"{glyphs().total} {self.total_elapsed()}", style="dim"),
        )
        renderables: list[RenderableType] = [header]
        groups: list[str] = []
        for phase in self._phases:
            if phase.group not in groups:
                groups.append(phase.group)
        show_rules: bool = len([group for group in groups if group]) > 1
        for group in groups:
            members = [phase for phase in self._phases if phase.group == group]
            if show_rules and group:
                renderables.append(Text(group, style="dim"))
            renderables.append(self._rows(members))
        return Group(*renderables)


@contextmanager
def planning_status(console: Console | None = None, text: str = "planning…") -> Generator[None]:
    """Show a transient status spinner (silent when not a terminal).

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
    spinner = Spinner(glyphs().spinner, text=Text(text, style="meta"), style="building")
    live = Live(spinner, console=active, refresh_per_second=12.5, transient=True)
    live.start(refresh=True)
    try:
        yield
    finally:
        live.stop()
