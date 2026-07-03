"""``billet host`` commands: up / stop / pin-ip / specs.

The composition root: constructs the concrete ``SubprocessRunner`` + ``AzureVmHostProvider``
and injects them into :class:`HostManager` (which only ever sees the ``HostProvider``
Protocol). dry-run rendering and the billable-create confirm gate live here, in the client.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

import typer

from billet.access.host.azure_vm_provider import AzureVmHostProvider
from billet.access.metrics.ssh_metrics_access import SshMetricsAccess
from billet.access.registry.toml_registry_access import RegistryAccess
from billet.cli import _planio
from billet.contracts import HostMetrics, HostProvider, HostSpec, HostStatus, MetricsAccess
from billet.host.manager.host_manager import HostManager
from billet.infrastructure.process import SubprocessRunner
from billet.shared.errors import BilletError

app = typer.Typer(name="host", help="Manage cloud Host (VM) lifecycle.", no_args_is_help=True)

ProviderFactory = Callable[[str], HostProvider]
MetricsFactory = Callable[[], MetricsAccess]


def _default_provider_factory(subscription_id: str) -> HostProvider:
    return AzureVmHostProvider(SubprocessRunner(), subscription_id=subscription_id)


def _default_metrics_factory() -> MetricsAccess:
    return SshMetricsAccess(SubprocessRunner())


# Replaced by tests with fake factories so the CLI is exercised without `az` / `ssh`.
provider_factory: ProviderFactory = _default_provider_factory
metrics_factory: MetricsFactory = _default_metrics_factory


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


def _build(config: Path | None, host: str | None) -> tuple[HostManager, HostSpec, str]:
    registry = RegistryAccess.resolve(config)
    key = registry.resolve_host_key(host)
    spec = registry.host(key)
    subscription_id = registry.global_config().subscription_id
    return HostManager(provider_factory(subscription_id)), spec, key


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
        if _planio.should_apply(plan, dry_run=dry_run, yes=yes):
            manager.apply(plan, spec)
            typer.echo(f"[billet] host '{key}' is up.")
    except BilletError as exc:
        _planio.fail(exc)


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
        if _planio.should_apply(plan, dry_run=dry_run, yes=yes):
            manager.apply(plan, spec)
            typer.echo(f"[billet] host '{key}' is deallocated.")
    except BilletError as exc:
        _planio.fail(exc)


@app.command()
def specs(
    host: _HostOption = None,
    config: _ConfigOption = None,
) -> None:
    """Report a running Host's live CPU / memory / disk / container usage."""
    try:
        manager, spec, key = _build(config, host)
        status, metrics = manager.read_metrics(spec, metrics_factory())
        _render_specs(key, spec, status, metrics)
    except BilletError as exc:
        _planio.fail(exc)


def _gib(size_bytes: int) -> str:
    return f"{size_bytes / 2**30:.1f} GiB"


def _render_specs(key: str, spec: HostSpec, status: HostStatus, metrics: HostMetrics) -> None:
    typer.echo(
        f"[billet] host '{key}' — {spec.vm_name} ({spec.vm_size}), "
        f"{status.raw_power}, ip {status.public_ip}"
    )
    cpu = metrics.cpu
    typer.echo(
        f"  cpu:  {cpu.cores} cores, "
        f"load {cpu.load_1m:.2f} / {cpu.load_5m:.2f} / {cpu.load_15m:.2f} (1/5/15 min)"
    )
    mem = metrics.memory
    typer.echo(
        f"  mem:  {_gib(mem.used_bytes)} used of {_gib(mem.total_bytes)} "
        f"({_gib(mem.available_bytes)} available, {mem.used_percent:.0f}% used)"
    )
    for disk in metrics.disks:
        typer.echo(
            f"  disk: {disk.mount} — {_gib(disk.used_bytes)} used of {_gib(disk.size_bytes)} "
            f"({_gib(disk.available_bytes)} free, {disk.used_percent:.0f}% used)"
        )
    if not metrics.containers:
        typer.echo("  containers: none running")
        return
    typer.echo(f"  containers ({len(metrics.containers)} running):")
    for container in metrics.containers:
        typer.echo(
            f"    {container.name:<24} cpu {container.cpu_percent:>8}  "
            f"mem {container.mem_usage} ({container.mem_percent})  [{container.status}]"
        )


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
        if _planio.should_apply(plan, dry_run=dry_run, yes=True):
            manager.apply(plan, spec)
            typer.echo(f"[billet] host '{key}' inbound SSH re-pinned.")
    except BilletError as exc:
        _planio.fail(exc)
