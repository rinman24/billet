"""Shared client-side plan rendering, the billable-create gate, and error reporting.

Keeping dry-run / confirm at the client (ADR-0001 §4) means these helpers — not the
managers — render plans, prompt, and turn a :class:`BilletError` into a clean exit. Used by
both the host and workspace command groups. All rendering delegates to :mod:`billet.cli._ui`;
the public names here are kept stable for their importers.
"""

from collections.abc import Callable
from dataclasses import dataclass
from typing import NoReturn

import typer

from billet.cli import _ui
from billet.contracts import Plan, PlanObserver, WorkspacePlan
from billet.shared.errors import BilletError


@dataclass(frozen=True, slots=True)
class GateCopy:
    """Command-context copy for the plan gate.

    ``vm_size`` names the size in the billable confirm preamble; ``already`` names the
    state an empty plan leaves the host in (e.g. ``"deallocated"``) so the
    nothing-to-do line can say so. Both are optional — omit what the caller cannot know.
    """

    vm_size: str | None = None
    already: str | None = None


def fail(exc: BilletError) -> NoReturn:
    """Render the error view for ``exc`` on stderr and exit 1 (no traceback)."""
    _ui.render_error(exc)
    raise typer.Exit(1)


def render_plan(plan: Plan, already: str | None = None) -> None:
    """Render a host :class:`Plan` for the operator.

    ``already`` names the state an empty plan leaves the host in (e.g. ``"deallocated"``)
    so the nothing-to-do line can say so; omit it when the caller cannot know.
    """
    _ui.render_host_plan(plan, already=already)


def should_apply(plan: Plan, *, dry_run: bool, yes: bool, copy: GateCopy | None = None) -> bool:
    """Render a host plan and decide whether to execute it (dry-run / billable confirm)."""
    gate = copy if copy is not None else GateCopy()
    render_plan(plan, already=gate.already)
    if dry_run:
        _ui.info("dry-run — no changes made")
        return False
    if plan.is_empty:
        return False
    if plan.is_billable and not yes:
        detail = f" ({gate.vm_size.lower()})" if gate.vm_size else ""
        _ui.caution(f"this creates a billable vm{detail}")
        if not typer.confirm(f"{_ui.GLYPH_PROMPT} proceed?"):
            _ui.info("aborted — no changes made")
            raise typer.Exit(1)
    return True


def run_plan(
    plan: Plan,
    *,
    dry_run: bool,
    yes: bool,
    apply: Callable[[PlanObserver], object],
    copy: GateCopy | None = None,
) -> bool:
    """Gate a host plan, then execute it with live progress; True when it applied.

    The gate (:func:`should_apply` — render, dry-run, billable confirm) runs *before*
    the Live display starts, so the ``typer.confirm`` prompt is never painted over.
    ``apply`` is the manager call bound to everything except the observer; host and
    workspace apply signatures differ, so the thunk keeps this helper shape-agnostic.
    """
    if not should_apply(plan, dry_run=dry_run, yes=yes, copy=copy):
        return False
    with _ui.PlanRenderer(plan.steps) as observer:
        apply(observer)
    return True


def render_workspace_plan(plan: WorkspacePlan) -> None:
    """Render a :class:`WorkspacePlan` for the operator."""
    _ui.render_workspace_plan(plan)


def run_workspace_plan(plan: WorkspacePlan, *, apply: Callable[[PlanObserver], object]) -> None:
    """Render a workspace plan, then execute it with live progress (no-op when empty).

    Workspace plans carry no billable gate, so there is no prompt: render, then hand a
    :class:`~billet.cli._ui.PlanRenderer` to the manager via the ``apply`` thunk.
    """
    render_workspace_plan(plan)
    if plan.is_empty:
        return
    with _ui.PlanRenderer(plan.steps) as observer:
        apply(observer)
