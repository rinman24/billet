"""Workspace data contracts and the Workspace-subsystem service Protocols.

The boundary here is deliberate (ADR-0002). :class:`WorkspaceSpec` carries only billet's
*operator intent* for a Workspace — which host, the loopback port billet assigns, the ssh
aliases, the tmux session, bootstrap hooks. The repo's *container facts* (service, compose
file, workspace folder, ``remoteUser``, postCreate) are NOT duplicated here: they live in
the repo's ``.devcontainer/devcontainer.json`` and are read live into
:class:`DevcontainerFacts` by ``ContainerAccess``. The two change for different reasons, on
different cadences, authored by different people — so they have different homes.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """Operator intent for one Workspace, parsed from a ``[workspaces.<key>]`` table.

    Only billet-owned intent lives here. Anything that is a property of the repo's
    container definition is read from ``devcontainer.json`` into :class:`DevcontainerFacts`.

    ``status_color`` is likewise operator intent — the optional hex brand color billet
    paints the Workspace's tmux status bar with on ``connect`` (its *status branding*), so an
    operator can tell otherwise-identical container shells apart. It belongs here, not in
    :class:`DevcontainerFacts`: the repo's container knows nothing about how billet brands it.
    """

    key: str
    host: str
    repo_url: str
    repo_dir: str
    container_ssh_port: int
    host_alias: str
    container_alias: str
    tmux_session: str
    agent_teams_flag: str
    host_bootstrap_cmd: str
    verify_cmd: str
    status_color: str | None = None


@dataclass(frozen=True, slots=True)
class DevcontainerFacts:
    """Facts read from a repo's ``.devcontainer/devcontainer.json`` (a read-only contract).

    ``compose_files`` are normalized to repo-root-relative paths (devcontainer.json declares
    ``dockerComposeFile`` relative to the ``.devcontainer/`` folder). ``post_create_command``
    is normalized to a single shell string (or ``None`` when the repo declares none).
    """

    service: str
    compose_files: tuple[str, ...]
    workspace_folder: str
    remote_user: str
    post_create_command: str | None


@dataclass(frozen=True, slots=True)
class RemoteHost:
    """The live connection facts for a Host — where the access layer sends ssh/compose."""

    admin_user: str
    ip: str


@dataclass(frozen=True, slots=True)
class WorkspaceStatus:
    """Live state of one Workspace for ``billet ls``."""

    key: str
    host: str
    container_alias: str
    running: bool


class WorkspaceStepKind(Enum):
    """A Workspace lifecycle operation the manager can schedule."""

    ENSURE_SOURCE = "ensure_source"
    COMPOSE_UP = "compose_up"
    POST_CREATE = "post_create"
    PERSONAL_BOOTSTRAP = "personal_bootstrap"
    VERIFY = "verify"
    COMPOSE_STOP = "compose_stop"


@dataclass(frozen=True, slots=True)
class WorkspacePlanStep:
    """One scheduled Workspace operation plus a human-readable summary for dry-run."""

    kind: WorkspaceStepKind
    summary: str


@dataclass(frozen=True, slots=True)
class WorkspacePlan:
    """An ordered set of Workspace steps with empty introspection."""

    workspace_key: str
    steps: tuple[WorkspacePlanStep, ...]

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to do."""
        return not self.steps


@dataclass(frozen=True, slots=True)
class SshConfigBlock:
    """Everything ``SshConfigEngine`` needs to render one Workspace's ssh-config entries.

    Assembled by the manager from a :class:`WorkspaceSpec`, its :class:`DevcontainerFacts`,
    and the host's live IP. Pure render input — it holds no behavior.
    """

    host_alias: str
    host_ip: str
    admin_user: str
    container_alias: str
    container_port: int
    container_user: str
    host_key_alias: str


class SourceAccess(Protocol):
    """Places the repo's source on the Host (agent-forwarded clone / fetch)."""

    def ensure_clone(self, spec: WorkspaceSpec, remote: RemoteHost) -> None:
        """Clone ``repo_url`` into ``repo_dir`` on the host, or fetch if already present."""
        ...


class ContainerAccess(Protocol):
    """Reads ``devcontainer.json`` and drives the repo's compose stack over SSH."""

    def read_facts(self, spec: WorkspaceSpec, remote: RemoteHost) -> DevcontainerFacts:
        """Read + parse the repo's ``devcontainer.json`` on the host into facts."""
        ...

    def compose_up(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> None:
        """Run the host bootstrap hook, write the agent-teams flag, then ``compose up -d --build``."""
        ...

    def run_post_create(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        """Run the devcontainer ``postCreateCommand`` inside the service container."""
        ...

    def run_personal_bootstrap(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts, command: str
    ) -> None:
        """Run the operator's ``personal_bootstrap_cmd`` inside the service container."""
        ...

    def verify(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> None:
        """Run the Workspace's ``verify_cmd`` inside the service container."""
        ...

    def compose_stop(
        self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts
    ) -> None:
        """Stop the compose stack (non-destructive — named volumes/data persist)."""
        ...

    def is_running(self, spec: WorkspaceSpec, remote: RemoteHost, facts: DevcontainerFacts) -> bool:
        """Return whether the service container is currently running."""
        ...


class SshConfigAccess(Protocol):
    """Owns the tool-managed ssh-config: writes ``billet.conf`` + ensures one ``Include``."""

    def write_conf(self, content: str) -> str:
        """Write the rendered ``config.d/billet.conf`` (0600); return its path as a string."""
        ...

    def ensure_include(self) -> None:
        """Ensure ``~/.ssh/config`` has exactly one ``Include`` line for ``billet.conf``."""
        ...
