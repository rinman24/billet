"""WorkspaceManager — orchestrates a Workspace's lifecycle behind the access Protocols.

The manager *plans* (composes a pure :class:`WorkspacePlan`) and *applies* (drives the
``SourceAccess`` / ``ContainerAccess`` seams). It reads the repo's container facts from
``devcontainer.json`` at apply time (the data contract — ADR-0002) rather than from config.

It deliberately does **not** call ``HostManager``: bringing the Host up is orchestrated by
the CLI client so the billable cold-create gate stays where the human is (ADR-0001 §4,
refined in ADR-0002). ``connect`` hands back an argv for the client to ``exec`` — the
manager never replaces the process itself, which keeps it unit-testable.
"""

from collections.abc import Callable, Sequence
import shlex

from billet.contracts import (
    ContainerAccess,
    DevcontainerFacts,
    HostSpec,
    NullPlanObserver,
    PlanObserver,
    RemoteHost,
    SourceAccess,
    SshConfigAccess,
    SshConfigBlock,
    WorkspacePlan,
    WorkspacePlanStep,
    WorkspaceSpec,
    WorkspaceStatus,
    WorkspaceStepKind,
)
from billet.infrastructure import ssh
from billet.shared.errors import BilletError, HostOperationError
from billet.workspace.engine.placement import HostPlacementPolicy
from billet.workspace.engine.port_allocator import PortAllocator
from billet.workspace.engine.ssh_config_engine import SshConfigEngine
from billet.workspace.engine.tmux_status_engine import TmuxStatusEngine


class WorkspaceManager:
    """Plans and applies the lifecycle of one Workspace through injected access seams."""

    def __init__(
        self,
        source: SourceAccess,
        container: ContainerAccess,
        ssh_config: SshConfigAccess,
    ) -> None:
        self._source = source
        self._container = container
        self._ssh_config = ssh_config
        self._allocator = PortAllocator()
        self._placement = HostPlacementPolicy()
        self._engine = SshConfigEngine()
        self._tmux_status = TmuxStatusEngine()

    # --- placement (command-verb precondition, ADR-0004) ---------------------------

    def assert_placement(self, host: HostSpec) -> None:
        """Raise ``ConfigError`` if ``host`` does not manage Workspaces.

        The command verbs (``add``/``start``/``stop``/``connect``/``ssh-config``) call this
        after resolving a Workspace's Host, so a Workspace can never be placed on a Host with
        ``manages_workspaces = false`` (e.g. the fleet-host). ``billet host`` verbs never call
        it, so a non-managing Host's VM lifecycle is unaffected. ``billet ls`` (a query) reads
        the flag for its projection instead (ADR-0004 §2).
        """
        self._placement.assert_manages_workspaces(host)

    # --- register (billet add) -----------------------------------------------------

    def register(self, spec: WorkspaceSpec, existing: Sequence[WorkspaceSpec]) -> str:
        """Validate a new Workspace and render its ``[workspaces.<key>]`` config block.

        billet stays stateless: ``register`` never writes ``config.toml`` — it validates
        (port free on the host) and returns the block for the operator to paste in.
        """
        self._allocator.assert_unique([*existing, spec])
        return _render_block(spec)

    # --- start ---------------------------------------------------------------------

    def plan_start(
        self, spec: WorkspaceSpec, *, verify: bool, personal_bootstrap_cmd: str = ""
    ) -> WorkspacePlan:
        """Build the (idempotent) plan to clone, build, bootstrap, and optionally verify."""
        steps = [
            WorkspacePlanStep(
                WorkspaceStepKind.ENSURE_SOURCE,
                f"clone / fast-forward {spec.repo_url} into {spec.repo_dir} on host '{spec.host}'",
            ),
            WorkspacePlanStep(
                WorkspaceStepKind.COMPOSE_UP,
                "docker compose up -d --build (service from devcontainer.json)",
            ),
            WorkspacePlanStep(
                WorkspaceStepKind.POST_CREATE,
                "run the devcontainer postCreateCommand in the service container",
            ),
        ]
        if personal_bootstrap_cmd:
            steps.append(
                WorkspacePlanStep(
                    WorkspaceStepKind.PERSONAL_BOOTSTRAP,
                    f"run personal bootstrap ({personal_bootstrap_cmd}) "
                    "in the service container (agent-forwarded)",
                )
            )
        if verify:
            steps.append(
                WorkspacePlanStep(
                    WorkspaceStepKind.VERIFY,
                    f"run verify command ({spec.verify_cmd}) in the service container",
                )
            )
        return WorkspacePlan(workspace_key=spec.key, steps=tuple(steps))

    def apply_start(  # noqa: PLR0913 — start threads each operator input explicitly (no bag)
        self,
        plan: WorkspacePlan,
        spec: WorkspaceSpec,
        remote: RemoteHost,
        *,
        personal_bootstrap_cmd: str,
        claude_oauth_token: str | None = None,
        observer: PlanObserver | None = None,
    ) -> DevcontainerFacts:
        """Execute a start plan; return the facts read from the repo's devcontainer.json.

        ``personal_bootstrap_cmd`` is required so plan/apply coupling stays explicit: pass
        the same value the plan was built with (empty disables the phase in both places).

        ``claude_oauth_token`` (already resolved by the caller from ``claude_token_cmd``) is
        threaded to ``compose_up`` for the in-container settings.json injection (ADR-0006);
        ``None``/empty skips injection. It is never stored on the manager.

        The ``observer`` receives semantic started/succeeded/failed events per step (the
        manager still never prints); a failed step re-raises and aborts the remainder.
        """
        obs: PlanObserver = observer if observer is not None else NullPlanObserver()
        facts: DevcontainerFacts | None = None

        def read_facts_once() -> DevcontainerFacts:
            nonlocal facts
            if facts is None:
                facts = self._container.read_facts(spec, remote)
            return facts

        for step in plan.steps:
            obs.step_started(step)
            try:
                self._dispatch_start(
                    step.kind,
                    spec,
                    remote,
                    read_facts_once,
                    personal_bootstrap_cmd,
                    claude_oauth_token,
                )
            except Exception:
                obs.step_failed(step)
                raise
            obs.step_succeeded(step)
        return read_facts_once()

    def _dispatch_start(  # noqa: PLR0913 — one dispatch arm per step; inputs stay explicit
        self,
        kind: WorkspaceStepKind,
        spec: WorkspaceSpec,
        remote: RemoteHost,
        facts: Callable[[], DevcontainerFacts],
        personal_bootstrap_cmd: str,
        claude_oauth_token: str | None,
    ) -> None:
        if kind is WorkspaceStepKind.ENSURE_SOURCE:
            self._source.ensure_clone(spec, remote)
        elif kind is WorkspaceStepKind.COMPOSE_UP:
            self._container.compose_up(spec, remote, facts(), claude_oauth_token)
        elif kind is WorkspaceStepKind.POST_CREATE:
            self._container.run_post_create(spec, remote, facts())
        elif kind is WorkspaceStepKind.PERSONAL_BOOTSTRAP:
            self._container.run_personal_bootstrap(spec, remote, facts(), personal_bootstrap_cmd)
        elif kind is WorkspaceStepKind.VERIFY:
            self._container.verify(spec, remote, facts())

    # --- stop ----------------------------------------------------------------------

    def plan_stop(self, spec: WorkspaceSpec) -> WorkspacePlan:
        """Build the plan to stop the Workspace's compose stack (non-destructive)."""
        return WorkspacePlan(
            workspace_key=spec.key,
            steps=(
                WorkspacePlanStep(
                    WorkspaceStepKind.COMPOSE_STOP, "docker compose stop (volumes/data persist)"
                ),
            ),
        )

    def apply_stop(
        self,
        plan: WorkspacePlan,
        spec: WorkspaceSpec,
        remote: RemoteHost,
        observer: PlanObserver | None = None,
    ) -> None:
        """Execute a stop plan, emitting started/succeeded/failed events per step."""
        obs: PlanObserver = observer if observer is not None else NullPlanObserver()
        step = next((s for s in plan.steps if s.kind is WorkspaceStepKind.COMPOSE_STOP), None)
        if step is None:
            return
        obs.step_started(step)
        try:
            facts = self._container.read_facts(spec, remote)
            self._container.compose_stop(spec, remote, facts)
        except Exception:
            obs.step_failed(step)
            raise
        obs.step_succeeded(step)

    # --- connect -------------------------------------------------------------------

    def read_facts(self, spec: WorkspaceSpec, remote: RemoteHost) -> DevcontainerFacts:
        """Read the repo's devcontainer.json facts from the host (used by connect)."""
        return self._container.read_facts(spec, remote)

    def connect_target(self, spec: WorkspaceSpec, facts: DevcontainerFacts) -> list[str]:
        """Return the ``ssh -t`` argv that lands the operator in a tmux session.

        Mirrors the lifted ``connect.sh``: a plain ``ssh -t`` through the container alias
        (which the ssh-config resolves via ``ProxyJump`` with the agent forwarded), attaching
        to — or creating — a single named tmux session at the workspace folder.

        The locale is pinned to ``C.UTF-8``: sshd sessions do not inherit the container
        image's Docker ``ENV``, and a client-forwarded ``LANG``/``LC_*`` (macOS sends them
        by default) usually names a locale the container never generated — either way tmux
        starts non-UTF-8 and renders every non-ASCII cell as ``_``. ``C.UTF-8`` is built
        into glibc, needs no locale-gen, and overrides any forwarded ``LC_*`` via
        ``LC_ALL``, so tmux and everything inside it always run UTF-8.

        ``TERM`` is pinned to ``xterm-256color`` for the same reason: ssh forwards the
        client's ``TERM``, and newer terminal emulators (Ghostty's ``xterm-ghostty``,
        kitty's ``xterm-kitty``) name terminfo entries most container images do not
        ship — tmux then aborts with ``missing or unsuitable terminal`` before the
        session ever starts. ``xterm-256color`` exists in every ncurses base install
        and is all tmux needs; the pin applies only to this remote command, so the
        operator's local terminal is untouched.

        The session's status bar is *branded* via a ``set -g`` prelude
        (:class:`TmuxStatusEngine`): the Workspace key is always shown on the left, and the
        optional ``status_color`` tints the bar so an operator can tell otherwise-identical
        container shells apart. The prelude runs on the same ``tmux`` invocation *ahead* of
        ``new-session`` because ``status-*`` are session globals and the attaching
        ``new-session -A`` short-circuits to an attach (never re-applying trailing options)
        when the session already exists — so branding must precede it to cover both the
        create and the re-attach path.
        """
        prelude = self._tmux_status.render_prelude(label=spec.key, color=spec.status_color)
        remote_command = (
            f"cd {shlex.quote(facts.workspace_folder)} && "
            "exec env LC_ALL=C.UTF-8 LANG=C.UTF-8 TERM=xterm-256color "
            f"tmux {prelude}new-session -A -s {shlex.quote(spec.tmux_session)} bash -l"
        )
        # user=None: the container alias in ssh-config already supplies user/host/port.
        return ssh.ssh_argv(None, spec.container_alias, remote_command, tty=True)

    # --- status (billet ls) --------------------------------------------------------

    def status_all(
        self, items: Sequence[tuple[WorkspaceSpec, RemoteHost]]
    ) -> list[WorkspaceStatus]:
        """Return the live state of each Workspace; an unreachable Host is reported as such."""
        statuses: list[WorkspaceStatus] = []
        for spec, remote in items:
            running, reachable = self._probe(spec, remote)
            statuses.append(
                WorkspaceStatus(
                    key=spec.key,
                    host=spec.host,
                    container_alias=spec.container_alias,
                    running=running,
                    reachable=reachable,
                )
            )
        return statuses

    def _probe(self, spec: WorkspaceSpec, remote: RemoteHost) -> tuple[bool, bool]:
        """Return ``(running, reachable)`` without letting one bad Host abort the query.

        ``HostOperationError`` marks the Host itself unreachable (SSH transport failure);
        any other ``BilletError`` (e.g. repo not cloned yet) means reachable-but-not-running.
        """
        try:
            facts = self._container.read_facts(spec, remote)
            return self._container.is_running(spec, remote, facts), True
        except HostOperationError:
            return False, False
        except BilletError:
            return False, True

    # --- ssh-config ----------------------------------------------------------------

    def render_ssh_config(self, blocks: Sequence[SshConfigBlock]) -> str:
        """Render the ``billet.conf`` body for ``blocks`` (pure)."""
        return self._engine.render_conf(blocks)

    def install_ssh_config(self, blocks: Sequence[SshConfigBlock]) -> str:
        """Render + write ``billet.conf`` and ensure the one ``Include`` line; return its path."""
        path = self._ssh_config.write_conf(self._engine.render_conf(blocks))
        self._ssh_config.ensure_include()
        return path


_STRING_FIELDS = (
    "host",
    "repo_url",
    "repo_dir",
    "host_alias",
    "container_alias",
    "tmux_session",
    "agent_teams_flag",
    "host_bootstrap_cmd",
    "verify_cmd",
)


def _render_block(spec: WorkspaceSpec) -> str:
    """Render a paste-ready ``[workspaces.<key>]`` TOML block for ``spec``."""
    lines = [f"[workspaces.{spec.key}]"]
    for field in _STRING_FIELDS:
        value: str = getattr(spec, field)
        lines.append(f'{field} = "{value}"')
    lines.append(f"container_ssh_port = {spec.container_ssh_port}")
    if spec.status_color is not None:
        lines.append(f'status_color = "{spec.status_color}"')
    return "\n".join(lines) + "\n"
