"""WorkspaceManager — orchestrates a Workspace's lifecycle behind the access Protocols.

The manager *plans* (composes a pure :class:`WorkspacePlan`) and *applies* (drives the
``SourceAccess`` / ``ContainerAccess`` seams). It reads the repo's container facts from
``devcontainer.json`` at apply time (the data contract — ADR-0002) rather than from config.

It deliberately does **not** call ``HostManager``: bringing the Host up is orchestrated by
the CLI client so the billable cold-create gate stays where the human is (ADR-0001 §4,
refined in ADR-0002). ``connect`` hands back an argv for the client to ``exec`` — the
manager never replaces the process itself, which keeps it unit-testable.
"""

from collections.abc import Sequence
import shlex

from billet.contracts import (
    ContainerAccess,
    DevcontainerFacts,
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
from billet.shared.errors import BilletError
from billet.workspace.engine.port_allocator import PortAllocator
from billet.workspace.engine.ssh_config_engine import SshConfigEngine


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
        self._engine = SshConfigEngine()

    # --- register (billet add) -----------------------------------------------------

    def register(self, spec: WorkspaceSpec, existing: Sequence[WorkspaceSpec]) -> str:
        """Validate a new Workspace and render its ``[workspaces.<key>]`` config block.

        billet stays stateless: ``register`` never writes ``config.toml`` — it validates
        (port free on the host) and returns the block for the operator to paste in.
        """
        self._allocator.assert_unique([*existing, spec])
        return _render_block(spec)

    # --- start ---------------------------------------------------------------------

    def plan_start(self, spec: WorkspaceSpec, *, verify: bool) -> WorkspacePlan:
        """Build the (idempotent) plan to clone, build, bootstrap, and optionally verify."""
        steps = [
            WorkspacePlanStep(
                WorkspaceStepKind.ENSURE_SOURCE,
                f"clone/fetch {spec.repo_url} into {spec.repo_dir} on host '{spec.host}'",
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
        if verify:
            steps.append(
                WorkspacePlanStep(
                    WorkspaceStepKind.VERIFY,
                    f"run verify command ({spec.verify_cmd}) in the service container",
                )
            )
        return WorkspacePlan(workspace_key=spec.key, steps=tuple(steps))

    def apply_start(
        self, plan: WorkspacePlan, spec: WorkspaceSpec, remote: RemoteHost
    ) -> DevcontainerFacts:
        """Execute a start plan; return the facts read from the repo's devcontainer.json."""
        kinds = {step.kind for step in plan.steps}
        if WorkspaceStepKind.ENSURE_SOURCE in kinds:
            self._source.ensure_clone(spec, remote)
        facts = self._container.read_facts(spec, remote)
        if WorkspaceStepKind.COMPOSE_UP in kinds:
            self._container.compose_up(spec, remote, facts)
        if WorkspaceStepKind.POST_CREATE in kinds:
            self._container.run_post_create(spec, remote, facts)
        if WorkspaceStepKind.VERIFY in kinds:
            self._container.verify(spec, remote, facts)
        return facts

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

    def apply_stop(self, plan: WorkspacePlan, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        """Execute a stop plan."""
        if not any(step.kind is WorkspaceStepKind.COMPOSE_STOP for step in plan.steps):
            return
        facts = self._container.read_facts(spec, remote)
        self._container.compose_stop(spec, remote, facts)

    # --- connect -------------------------------------------------------------------

    def read_facts(self, spec: WorkspaceSpec, remote: RemoteHost) -> DevcontainerFacts:
        """Read the repo's devcontainer.json facts from the host (used by connect)."""
        return self._container.read_facts(spec, remote)

    def connect_target(self, spec: WorkspaceSpec, facts: DevcontainerFacts) -> list[str]:
        """Return the ``ssh -t`` argv that lands the operator in a tmux session.

        Mirrors the lifted ``connect.sh``: a plain ``ssh -t`` through the container alias
        (which the ssh-config resolves via ``ProxyJump`` with the agent forwarded), attaching
        to — or creating — a single named tmux session at the workspace folder.
        """
        remote_command = (
            f"cd {shlex.quote(facts.workspace_folder)} && "
            f"exec tmux new-session -A -s {shlex.quote(spec.tmux_session)} bash -l"
        )
        # user=None: the container alias in ssh-config already supplies user/host/port.
        return ssh.ssh_argv(None, spec.container_alias, remote_command, tty=True)

    # --- status (billet ls) --------------------------------------------------------

    def status_all(
        self, items: Sequence[tuple[WorkspaceSpec, RemoteHost]]
    ) -> list[WorkspaceStatus]:
        """Return the live state of each Workspace; an unreachable Host reports not-running."""
        statuses: list[WorkspaceStatus] = []
        for spec, remote in items:
            statuses.append(
                WorkspaceStatus(
                    key=spec.key,
                    host=spec.host,
                    container_alias=spec.container_alias,
                    running=self._safe_is_running(spec, remote),
                )
            )
        return statuses

    def _safe_is_running(self, spec: WorkspaceSpec, remote: RemoteHost) -> bool:
        try:
            facts = self._container.read_facts(spec, remote)
            return self._container.is_running(spec, remote, facts)
        except BilletError:
            return False

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
    return "\n".join(lines) + "\n"
