"""``billet host`` commands: up / stop / pin-ip / specs.

The composition root: constructs the concrete ``SubprocessRunner`` + ``AzureVmHostProvider``
and injects them into :class:`HostManager` (which only ever sees the ``HostProvider``
Protocol). dry-run rendering and the billable-create confirm gate live here, in the client.
"""

from collections.abc import Callable
from pathlib import Path
from typing import Annotated

from rich import box
from rich.console import Console
from rich.padding import Padding
from rich.table import Table
from rich.text import Text
import typer

from billet.access.host.azure_vm_provider import AzureVmHostProvider
from billet.access.metrics.ssh_metrics_access import SshMetricsAccess
from billet.access.registry.toml_registry_access import RegistryAccess
from billet.cli import _planio
from billet.cli._ui import planning_status
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
        with planning_status():
            manager, spec, key = _build(config, host)
            plan = manager.plan_up(spec)
        if _planio.run_plan(
            plan, dry_run=dry_run, yes=yes, apply=lambda obs: manager.apply(plan, spec, obs)
        ):
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
        with planning_status():
            manager, spec, key = _build(config, host)
            plan = manager.plan_stop(spec)
        if _planio.run_plan(
            plan, dry_run=dry_run, yes=yes, apply=lambda obs: manager.apply(plan, spec, obs)
        ):
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


_console = Console(highlight=False)

_BAR_WIDTH = 10
_BAR_HOT_PERCENT = 85.0
_CPU_FILL = "#C05CE0"
_MEM_FILL = "#C05CE0"


def _gib(size_bytes: int) -> str:
    return f"{size_bytes / 2**30:.1f} GiB"


def _parse_percent(raw: str) -> float | None:
    """Parse a ``docker stats`` percentage like ``'7.7%'`` (None when unparseable)."""
    try:
        return float(raw.strip().removesuffix("%"))
    except ValueError:
        return None


def _usage_cell(percent: float | None, fill: str, fallback: str = "?") -> Text:
    """Render a fixed-width usage bar with its label, e.g. ``██░░░░░░░░  25.0%``."""
    if percent is None:
        return Text(f"{'':{_BAR_WIDTH}} {fallback:>6}")
    clamped: float = min(max(percent, 0.0), 100.0)
    filled: int = round(_BAR_WIDTH * clamped / 100.0)
    cell = Text("█" * filled, style="red" if clamped >= _BAR_HOT_PERCENT else fill)
    cell.append("░" * (_BAR_WIDTH - filled), style="dim")
    cell.append(f" {percent:5.1f}%")
    return cell


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
    mem_line = Text("  mem:  ")
    mem_line.append_text(_usage_cell(mem.used_percent, _MEM_FILL))
    mem_line.append(
        f"  {_gib(mem.used_bytes)} used of {_gib(mem.total_bytes)} "
        f"({_gib(mem.available_bytes)} available)"
    )
    _console.print(mem_line)
    for disk in metrics.disks:
        disk_line = Text("  disk: ")
        disk_line.append_text(_usage_cell(disk.used_percent, _MEM_FILL))
        disk_line.append(
            f"  {disk.mount} — {_gib(disk.used_bytes)} used of {_gib(disk.size_bytes)} "
            f"({_gib(disk.available_bytes)} free)"
        )
        _console.print(disk_line)
    if not metrics.containers:
        typer.echo("  containers: none running")
        return
    typer.echo(
        f"  containers ({len(metrics.containers)} running) — "
        f"bars show share of the whole host ({cpu.cores} cores):"
    )
    _console.print(Padding(_containers_table(metrics), (0, 0, 0, 2)))


def _containers_table(metrics: HostMetrics) -> Table:
    table = Table(box=box.SIMPLE_HEAD, show_edge=False, pad_edge=False, padding=(0, 2, 0, 0))
    cell_width: int = _BAR_WIDTH + 7  # bar + space + "nnn.n%"
    table.add_column("name", no_wrap=True)
    table.add_column("cpu", no_wrap=True, min_width=cell_width)
    table.add_column("mem", no_wrap=True, min_width=cell_width)
    table.add_column("mem used", no_wrap=True)
    table.add_column("status", no_wrap=True)
    cores: int = metrics.cpu.cores
    for container in metrics.containers:
        raw_cpu: float | None = _parse_percent(container.cpu_percent)
        # `docker stats` CPU% is per-core; divide by the core count for the host share.
        host_cpu: float | None = raw_cpu / cores if raw_cpu is not None and cores else None
        table.add_row(
            container.name,
            _usage_cell(host_cpu, _CPU_FILL, fallback=container.cpu_percent),
            _usage_cell(
                _parse_percent(container.mem_percent), _MEM_FILL, fallback=container.mem_percent
            ),
            # mem_usage is "1.2GiB / 15.6GiB"; the host total already heads the report.
            container.mem_usage.split(" / ")[0],
            container.status,
        )
    return table


@app.command(name="pin-ip")
def pin_ip(
    host: _HostOption = None,
    config: _ConfigOption = None,
    dry_run: _DryRunOption = False,
) -> None:
    """Re-pin the inbound SSH rule to your current egress IP/32 (no state change)."""
    try:
        with planning_status():
            manager, spec, key = _build(config, host)
            plan = manager.plan_pin_ip(spec)
        if _planio.run_plan(
            plan, dry_run=dry_run, yes=True, apply=lambda obs: manager.apply(plan, spec, obs)
        ):
            typer.echo(f"[billet] host '{key}' inbound SSH re-pinned.")
    except BilletError as exc:
        _planio.fail(exc)
