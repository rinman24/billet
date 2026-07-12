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
class Gate:
    """Everything the plan gate needs beyond the plan itself.

    ``dry_run`` / ``yes`` are the command flags. ``vm_size`` names the size in the
    billable confirm preamble; ``already`` names the state an empty plan leaves the
    host in (e.g. ``"deallocated"``) so the nothing-to-do line can say so. The last
    two are optional — omit what the caller cannot know.
    """

    dry_run: bool = False
    yes: bool = False
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


def should_apply(plan: Plan, *, gate: Gate) -> bool:
    """Decide whether to execute a host plan (dry-run / empty / billable confirm).

    Per the review-then-watch arc (§7.1–7.2) the plan view renders only when there is a
    decision to make: always under ``--dry-run``, and ahead of the billable confirm.
    A plain resume goes straight to the live checklist — the checklist *is* the plan.
    """
    if gate.dry_run:
        render_plan(plan, already=gate.already)
        _ui.info("dry-run — no changes made")
        return False
    if plan.is_empty:
        _ui.nothing_to_do(plan.host_key, gate.already)
        return False
    if plan.is_billable and not gate.yes:
        render_plan(plan)
        detail = f" ({gate.vm_size.lower()})" if gate.vm_size else ""
        _ui.caution(f"this creates a billable vm{detail}")
        if not typer.confirm(f"{_ui.glyphs().prompt} proceed?"):
            _ui.info("aborted — no changes made")
            raise typer.Exit(1)
    return True


def run_plan(
    plan: Plan,
    *,
    gate: Gate,
    apply: Callable[[PlanObserver], object],
    checklist: _ui.PhaseChecklist,
) -> bool:
    """Gate a host plan, then execute it under the live checklist; True when it applied.

    The gate (:func:`should_apply` — dry-run, empty, billable confirm) runs *before*
    the Live display starts, so the ``typer.confirm`` prompt is never painted over.
    ``apply`` is the manager call bound to everything except the observer; host and
    workspace apply signatures differ, so the thunk keeps this helper shape-agnostic.
    """
    if not should_apply(plan, gate=gate):
        return False
    with checklist as observer:
        apply(observer)
    return True


def render_workspace_plan(plan: WorkspacePlan) -> None:
    """Render a :class:`WorkspacePlan` for the operator."""
    _ui.render_workspace_plan(plan)


def run_workspace_plan(
    plan: WorkspacePlan,
    *,
    apply: Callable[[PlanObserver], object],
    checklist: _ui.PhaseChecklist,
) -> None:
    """Execute a workspace plan under the live checklist (no-op when empty).

    Workspace plans carry no billable gate, so there is no prompt and no plan view —
    the checklist itself narrates the work.
    """
    if plan.is_empty:
        _ui.nothing_to_do(plan.workspace_key)
        return
    with checklist as observer:
        apply(observer)
