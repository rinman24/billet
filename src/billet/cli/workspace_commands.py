"""Top-level ``billet`` Workspace commands: add / ls / start / stop / connect / ssh-config / rm.

The composition root for the Workspace subsystem: it constructs the concrete access
implementations (over ``SubprocessRunner``) and injects them into :class:`WorkspaceManager`,
which only ever sees the access Protocols. Bringing the Host up is orchestrated *here* (not
inside the manager) so the billable cold-create gate stays at the client (ADR-0001 §4 /
ADR-0002). ``connect`` hands the terminal off via ``os.execvp``.

These commands are registered on the root app at top level by :func:`register`.
"""

from collections.abc import Callable, Sequence
import os
from pathlib import Path
from typing import Annotated, NoReturn, Protocol

import typer

from billet.access.container.compose_container_access import ComposeContainerAccess
from billet.access.host.azure_vm_provider import AzureVmHostProvider
from billet.access.registry.toml_registry_access import RegistryAccess
from billet.access.source.git_source_access import GitSourceAccess
from billet.access.sshconfig.file_ssh_config_access import FileSshConfigAccess
from billet.cli import _planio, _ui
from billet.contracts import (
    HostProvider,
    HostSpec,
    RemoteHost,
    SshConfigBlock,
    WorkspaceSpec,
    WorkspaceStatus,
)
from billet.host.manager.host_manager import HostManager
from billet.infrastructure.process import OnLine, SubprocessRunner
from billet.shared.errors import BilletError, HostOperationError
from billet.workspace.manager.workspace_manager import WorkspaceManager

ProviderFactory = Callable[[str], HostProvider]


class WorkspaceManagerFactory(Protocol):
    """Builds a WorkspaceManager; ``on_line`` (optional) streams the compose-up output."""

    def __call__(self, on_line: OnLine | None = None) -> WorkspaceManager:
        """Return a manager wired over the concrete access implementations."""
        ...


class _ComposeLineSink:
    """A late-bound line sink for the compose-up stream.

    The access object is constructed (via the factory) before the checklist exists, so
    the sink is wired in first and ``target`` is pointed at the checklist once built.
    """

    def __init__(self) -> None:
        self.target: OnLine | None = None

    def __call__(self, line: str) -> None:
        """Forward ``line`` to the bound target (drop it while unbound)."""
        if self.target is not None:
            self.target(line)


def _default_provider_factory(subscription_id: str) -> HostProvider:
    return AzureVmHostProvider(SubprocessRunner(), subscription_id=subscription_id)


def _default_workspace_manager_factory(on_line: OnLine | None = None) -> WorkspaceManager:
    runner = SubprocessRunner()
    return WorkspaceManager(
        GitSourceAccess(runner),
        ComposeContainerAccess(runner, on_compose_line=on_line),
        FileSshConfigAccess(),
    )


# Replaced by tests with fakes so the CLI is exercised without `az` / `ssh`.
provider_factory: ProviderFactory = _default_provider_factory
workspace_manager_factory: WorkspaceManagerFactory = _default_workspace_manager_factory


def _execvp(argv: list[str]) -> NoReturn:
    """Replace this process with ``argv`` (the interactive ssh). Tests monkeypatch this."""
    os.execvp(argv[0], argv)


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
_VerifyOption = Annotated[
    bool, typer.Option("--verify", help="Run the verify command in the container after bootstrap.")
]
_KeyArgument = Annotated[str, typer.Argument(help="Workspace key (a [workspaces.<key>] table).")]


def _registry(config: Path | None) -> RegistryAccess:
    return RegistryAccess.resolve(config)


def _other_workspaces(registry: RegistryAccess, key: str) -> list[WorkspaceSpec]:
    return [registry.workspace(other) for other in registry.workspace_keys() if other != key]


def _remote_via_alias(host: HostSpec, ws: WorkspaceSpec) -> RemoteHost:
    """Reach the host through its ssh-config alias — no ``az`` call needed."""
    return RemoteHost(admin_user=host.admin_user, ip=ws.host_alias)


# --- add ---------------------------------------------------------------------------


def add(key: _KeyArgument, config: _ConfigOption = None) -> None:
    """Validate a Workspace declared in config.toml and echo its canonical block.

    billet is stateless: it never writes config.toml. ``add`` validates the operator-authored
    ``[workspaces.<key>]`` block (host exists, loopback port unique) and prints it back so the
    operator can confirm it is well-formed before ``billet start``.
    """
    try:
        registry = _registry(config)
        spec = registry.workspace(key)
        host = registry.host(spec.host)  # raises ConfigError if the referenced host is undefined
        manager = workspace_manager_factory()
        manager.assert_placement(host)  # ADR-0004: refuse a manages_workspaces=false host
        block = manager.register(spec, _other_workspaces(registry, key))
        _ui.block_panel(f"workspace {key} · valid", block, border_style="building")
        _ui.hint("start it", f"billet start {key}")
    except BilletError as exc:
        _planio.fail(exc)


# --- ls ----------------------------------------------------------------------------


def ls(config: _ConfigOption = None) -> None:
    """List registered Workspaces and whether each container is running."""
    try:
        registry = _registry(config)
        manager = workspace_manager_factory()
        workspaces = [registry.workspace(k) for k in registry.workspace_keys()]
        if not workspaces:
            _ui.empty_state(
                (
                    "no workspaces yet.",
                    "declare [workspaces.<key>] in config.toml,",
                    "then billet add <key>",
                )
            )
            return
        # ls is a query, not a command (ADR-0004 §2): a Workspace on a non-managing Host is
        # surfaced inline as invalid rather than raising, and only managing Hosts are probed.
        probeable = [
            (ws, _remote_via_alias(registry.host(ws.host), ws))
            for ws in workspaces
            if registry.host(ws.host).manages_workspaces
        ]
        statuses = {status.key: status for status in manager.status_all(probeable)}
        groups = [
            _ls_group(registry.host(host_key), workspaces, statuses)
            for host_key in registry.host_keys()
        ]
        _ui.render_ls(groups)
    except BilletError as exc:
        _planio.fail(exc)


def _ls_state(ws: WorkspaceSpec, host: HostSpec, statuses: dict[str, WorkspaceStatus]) -> str:
    if not host.manages_workspaces:
        return "invalid"
    if not statuses[ws.key].reachable:
        return "unreachable"
    return "running" if statuses[ws.key].running else "stopped"


def _ls_group(
    host: HostSpec,
    workspaces: Sequence[WorkspaceSpec],
    statuses: dict[str, WorkspaceStatus],
) -> _ui.LsHostGroup:
    rows = tuple(
        _ui.LsWorkspaceRow(
            key=ws.key,
            state=_ls_state(ws, host, statuses),
            alias=ws.container_alias,
            port=ws.container_ssh_port,
        )
        for ws in workspaces
        if ws.host == host.key
    )
    return _ui.LsHostGroup(
        key=host.key,
        vm_size=host.vm_size,
        manages_workspaces=host.manages_workspaces,
        rows=rows,
    )


# --- start -------------------------------------------------------------------------


def start(
    key: _KeyArgument,
    config: _ConfigOption = None,
    dry_run: _DryRunOption = False,
    yes: _YesOption = False,
    verify: _VerifyOption = False,
) -> None:
    """Bring the Host up (if needed), then clone, build, and bootstrap the Workspace."""
    try:
        compose_lines = _ComposeLineSink()
        with _ui.planning_status():
            registry = _registry(config)
            ws = registry.workspace(key)
            host = registry.host(ws.host)
            global_config = registry.global_config()
            provider = provider_factory(global_config.subscription_id)
            host_manager = HostManager(provider)
            manager = workspace_manager_factory(compose_lines)
            manager.assert_placement(host)  # ADR-0004: refuse manages_workspaces=false
            host_plan = host_manager.plan_up(host)
            ws_plan = manager.plan_start(
                ws, verify=verify, personal_bootstrap_cmd=global_config.personal_bootstrap_cmd
            )

        if dry_run:
            _planio.render_plan(host_plan)
            _planio.render_workspace_plan(ws_plan)
            _ui.info("dry-run — no changes made")
            return

        # The billable cold-create gate fires here, at the client, before any Live display.
        apply_host = _planio.should_apply(
            host_plan, gate=_planio.Gate(yes=yes, vm_size=host.vm_size)
        )

        # One checklist carries the whole posting arc: host phases, then workspace phases.
        checklist = _ui.PhaseChecklist(
            [
                *(_ui.host_phases(host_plan, host) if apply_host else []),
                *_ui.workspace_phases(ws_plan, ws),
            ],
            title=f"posting {key} → {ws.host}",
        )
        compose_lines.target = checklist.compose_tail()
        with checklist as observer:
            if apply_host:
                host_manager.apply(host_plan, host, observer)
            remote = _resolve_running_remote(provider, host)
            manager.apply_start(
                ws_plan,
                ws,
                remote,
                personal_bootstrap_cmd=global_config.personal_bootstrap_cmd,
                observer=observer,
            )
        _ui.success(f"workspace {key} is up", checklist.total_elapsed())
        _ui.next_hint("billet ssh-config", f"billet connect {key}")
    except BilletError as exc:
        _planio.fail(exc)


def _resolve_running_remote(provider: HostProvider, host: HostSpec) -> RemoteHost:
    ip = provider.status(host).public_ip
    if ip is None:
        raise HostOperationError(
            f"host '{host.key}' has no public IP — is it running? Run `billet host up` first."
        )
    return RemoteHost(admin_user=host.admin_user, ip=ip)


# --- stop --------------------------------------------------------------------------


def stop(key: _KeyArgument, config: _ConfigOption = None, dry_run: _DryRunOption = False) -> None:
    """Stop a Workspace's compose stack (non-destructive — volumes/data persist)."""
    try:
        with _ui.planning_status():
            registry = _registry(config)
            ws = registry.workspace(key)
            host = registry.host(ws.host)
            manager = workspace_manager_factory()
            manager.assert_placement(host)  # ADR-0004: refuse manages_workspaces=false
            plan = manager.plan_stop(ws)
        if dry_run:
            _planio.render_workspace_plan(plan)
            _ui.info("dry-run — no changes made")
            return
        _planio.run_workspace_plan(
            plan,
            apply=lambda obs: manager.apply_stop(plan, ws, _remote_via_alias(host, ws), obs),
            checklist=_ui.PhaseChecklist(
                _ui.workspace_phases(plan, ws), title=f"stop · workspace {key}"
            ),
        )
        _ui.success(f"workspace {key} stopped", "data persists")
    except BilletError as exc:
        _planio.fail(exc)


# --- connect -----------------------------------------------------------------------


def connect(key: _KeyArgument, config: _ConfigOption = None) -> None:
    """SSH into the Workspace container and attach to its tmux session."""
    try:
        registry = _registry(config)
        ws = registry.workspace(key)
        host = registry.host(ws.host)
        manager = workspace_manager_factory()
        manager.assert_placement(host)  # ADR-0004: refuse a manages_workspaces=false host
        facts = manager.read_facts(ws, _remote_via_alias(host, ws))
        argv = manager.connect_target(ws, facts)
    except BilletError as exc:
        _planio.fail(exc)
    _execvp(argv)


# --- ssh-config --------------------------------------------------------------------


def ssh_config(config: _ConfigOption = None, dry_run: _DryRunOption = False) -> None:
    """Render the tool-owned ssh-config Include file for every Workspace."""
    try:
        registry = _registry(config)
        subscription_id = registry.global_config().subscription_id
        provider = provider_factory(subscription_id)
        manager = workspace_manager_factory()
        provider.preflight()
        blocks: list[SshConfigBlock] = []
        for ws in (registry.workspace(k) for k in registry.workspace_keys()):
            host = registry.host(ws.host)
            manager.assert_placement(host)  # ADR-0004: refuse a manages_workspaces=false host
            blocks.append(_block_for(provider, manager, host, ws))
        if not blocks:
            _ui.empty_state(
                (
                    "no workspaces yet.",
                    "declare [workspaces.<key>] in config.toml,",
                    "then billet add <key>",
                )
            )
            return
        if dry_run:
            _ui.block_panel("billet.conf · dry-run", manager.render_ssh_config(blocks))
            _ui.info("dry-run — no changes made")
            return
        path = manager.install_ssh_config(blocks)
        _ui.success(f"wrote {path}", "ensured Include in ~/.ssh/config")
    except BilletError as exc:
        _planio.fail(exc)


def _block_for(
    provider: HostProvider, manager: WorkspaceManager, host: HostSpec, ws: WorkspaceSpec
) -> SshConfigBlock:
    ip = provider.status(host).public_ip
    if ip is None:
        raise HostOperationError(
            f"host '{host.key}' has no public IP — start it first with `billet start`."
        )
    remote = RemoteHost(admin_user=host.admin_user, ip=ip)
    facts = manager.read_facts(ws, remote)
    return SshConfigBlock(
        host_alias=ws.host_alias,
        host_ip=ip,
        admin_user=host.admin_user,
        container_alias=ws.container_alias,
        container_port=ws.container_ssh_port,
        container_user=facts.remote_user,
        host_key_alias=ws.container_alias,
    )


# --- rm ----------------------------------------------------------------------------


def rm(key: _KeyArgument, config: _ConfigOption = None) -> None:
    """Explain how to deregister a Workspace (billet never edits config.toml itself)."""
    try:
        registry = _registry(config)
        registry.workspace(key)  # raises if unknown
    except BilletError as exc:
        _planio.fail(exc)
    _ui.titled_steps(
        f"workspace {key} · deregister",
        "billet is stateless — it never edits config.toml.",
        (
            ("stop its container", f"billet stop {key}"),
            (f"delete the [workspaces.{key}] block from config.toml", ""),
            ("regenerate ssh-config", "billet ssh-config"),
        ),
    )


def register(app: typer.Typer) -> None:
    """Register the Workspace commands on the root ``billet`` app at top level."""
    panel = "workspace · devcontainers on a host"
    app.command(name="add", rich_help_panel=panel)(add)
    app.command(name="ls", rich_help_panel=panel)(ls)
    app.command(name="start", rich_help_panel=panel)(start)
    app.command(name="stop", rich_help_panel=panel)(stop)
    app.command(name="connect", rich_help_panel=panel)(connect)
    app.command(name="ssh-config", rich_help_panel=panel)(ssh_config)
    app.command(name="rm", rich_help_panel=panel)(rm)
