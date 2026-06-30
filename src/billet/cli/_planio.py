"""Shared client-side plan rendering, the billable-create gate, and error reporting.

Keeping dry-run / confirm at the client (ADR-0001 §4) means these helpers — not the
managers — render plans, prompt, and turn a :class:`BilletError` into a clean exit. Used by
both the host and workspace command groups.
"""

from typing import NoReturn

import typer

from billet.contracts import Plan, WorkspacePlan
from billet.shared.errors import BilletError


def fail(exc: BilletError) -> NoReturn:
    """Print a billet error and exit non-zero (no traceback)."""
    typer.secho(f"[billet] error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def render_plan(plan: Plan) -> None:
    """Render a host :class:`Plan` for the operator."""
    if plan.is_empty:
        typer.echo("[billet] nothing to do.")
        return
    typer.echo(f"[billet] plan for host '{plan.host_key}':")
    for step in plan.steps:
        typer.echo(f"  + {step.summary}")


def should_apply(plan: Plan, *, dry_run: bool, yes: bool) -> bool:
    """Render a host plan and decide whether to execute it (dry-run / billable confirm)."""
    render_plan(plan)
    if dry_run:
        typer.echo("[billet] dry-run: no changes made.")
        return False
    if plan.is_empty:
        return False
    if plan.is_billable and not yes:
        if not typer.confirm("[billet] This creates a billable VM. Proceed?"):
            typer.echo("[billet] aborted; no changes made.")
            raise typer.Exit(1)
    return True


def render_workspace_plan(plan: WorkspacePlan) -> None:
    """Render a :class:`WorkspacePlan` for the operator."""
    if plan.is_empty:
        typer.echo("[billet] nothing to do.")
        return
    typer.echo(f"[billet] plan for workspace '{plan.workspace_key}':")
    for step in plan.steps:
        typer.echo(f"  + {step.summary}")
