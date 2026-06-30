"""``billet host`` commands: up / stop / pin-ip.

The composition root: constructs the concrete ``SubprocessRunner`` + ``AzureVmHostProvider``
and injects them into :class:`HostManager` (which only ever sees the ``HostProvider``
Protocol). dry-run rendering and the billable-create confirm gate live here, in the client.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated, NoReturn

import typer

from billet.access.host.azure_vm_provider import AzureVmHostProvider
from billet.access.registry.toml_registry_access import RegistryAccess
from billet.contracts import HostProvider, HostSpec, Plan
from billet.host.manager.host_manager import HostManager
from billet.infrastructure.process import SubprocessRunner
from billet.shared.errors import BilletError

app = typer.Typer(name="host", help="Manage cloud Host (VM) lifecycle.", no_args_is_help=True)

ProviderFactory = Callable[[str], HostProvider]


def _default_provider_factory(subscription_id: str) -> HostProvider:
    return AzureVmHostProvider(SubprocessRunner(), subscription_id=subscription_id)


# Replaced by tests with a fake-provider factory so the CLI is exercised without `az`.
provider_factory: ProviderFactory = _default_provider_factory


_HostOption = Annotated[
    str | None, typer.Option("--host", help="Host key (default: [billet].default_host).")
]
_ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Path to config.toml (default: XDG / $BILLET_CONFIG)."),
]
_DryRunOption = Annotated[
    bool, typer.Option("--dry-run", help="Show the plan without making any changes.")
]
_YesOption = Annotated[
    bool, typer.Option("--yes", "-y", help="Skip the billable-create confirmation.")
]


def _fail(exc: BilletError) -> NoReturn:
    typer.secho(f"[billet] error: {exc}", fg=typer.colors.RED, err=True)
    raise typer.Exit(1)


def _build(config: Path | None, host: str | None) -> tuple[HostManager, HostSpec, str]:
    registry = RegistryAccess.resolve(config)
    key = registry.resolve_host_key(host)
    spec = registry.host(key)
    subscription_id = registry.global_config().subscription_id
    return HostManager(provider_factory(subscription_id)), spec, key


def _render(plan: Plan) -> None:
    if plan.is_empty:
        typer.echo("[billet] nothing to do.")
        return
    typer.echo(f"[billet] plan for host '{plan.host_key}':")
    for step in plan.steps:
        typer.echo(f"  + {step.summary}")


def _should_apply(plan: Plan, *, dry_run: bool, yes: bool) -> bool:
    """Render the plan and decide whether to execute it (handles dry-run / confirm)."""
    _render(plan)
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


@app.command()
def up(
    host: _HostOption = None,
    config: _ConfigOption = None,
    dry_run: _DryRunOption = False,
    yes: _YesOption = False,
) -> None:
    """Bring a Host up (cold provision or resume, auto-detected)."""
    try:
        manager, spec, key = _build(config, host)
        plan = manager.plan_up(spec)
        if _should_apply(plan, dry_run=dry_run, yes=yes):
            manager.apply(plan, spec)
            typer.echo(f"[billet] host '{key}' is up.")
    except BilletError as exc:
        _fail(exc)


@app.command()
def stop(
    host: _HostOption = None,
    config: _ConfigOption = None,
    dry_run: _DryRunOption = False,
    yes: _YesOption = False,
) -> None:
    """Deallocate a Host (stops compute billing; the OS disk persists)."""
    try:
        manager, spec, key = _build(config, host)
        plan = manager.plan_stop(spec)
        if _should_apply(plan, dry_run=dry_run, yes=yes):
            manager.apply(plan, spec)
            typer.echo(f"[billet] host '{key}' is deallocated.")
    except BilletError as exc:
        _fail(exc)


@app.command(name="pin-ip")
def pin_ip(
    host: _HostOption = None,
    config: _ConfigOption = None,
    dry_run: _DryRunOption = False,
) -> None:
    """Re-pin the inbound SSH rule to your current egress IP/32 (no state change)."""
    try:
        manager, spec, key = _build(config, host)
        plan = manager.plan_pin_ip(spec)
        if _should_apply(plan, dry_run=dry_run, yes=True):
            manager.apply(plan, spec)
            typer.echo(f"[billet] host '{key}' inbound SSH re-pinned.")
    except BilletError as exc:
        _fail(exc)
