"""Workspace data contract.

Parsed and validated by ``RegistryAccess`` in slice 4 so the schema is pinned and tested;
consumed by the Workspace subsystem (register / start / connect) from slice 5 onward.
"""

from dataclasses import dataclass


@dataclass(frozen=True, slots=True)
class WorkspaceSpec:
    """Operator intent for one Workspace, parsed from a ``[workspaces.<key>]`` table."""

    key: str
    host: str
    repo_url: str
    repo_dir: str
    compose_file: str
    service: str
    container_user: str
    container_ssh_port: int
    workspace_folder: str
    tmux_session: str
    host_alias: str
    container_alias: str
    agent_teams_flag: str
    host_bootstrap_cmd: str
    container_bootstrap_cmd: str
    verify_cmd: str
