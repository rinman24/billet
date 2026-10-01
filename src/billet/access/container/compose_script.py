"""The compose script pieces two access modules share: the data contract and the ps.

``ComposeContainerAccess`` (``start``, ``stop``, ``ls``) and ``SshDoctorAccess`` (``doctor``)
must parse ``devcontainer.json`` the same way and look up the running service with the same
``docker compose ps``, under the same prelude, so ``doctor`` names the service and compose
files ``start`` drives. This leaf module holds exactly those pieces. It imports no other
access module, and it is the only access module another access module may import (the
``access modules are independent`` import-linter contract).
"""

import posixpath
import shlex
from typing import Any, cast

from billet.contracts import DevcontainerFacts, RemoteHost, WorkspaceSpec
from billet.shared import jsonc
from billet.shared.errors import ConfigError

#: The data contract billet reads, relative to a Workspace's ``repo_dir`` on the Host.
DEVCONTAINER_JSON = ".devcontainer/devcontainer.json"
_DEVCONTAINER_DIR = ".devcontainer"


def _as_str_list(value: Any, what: str) -> list[str]:
    """Coerce a JSON string-or-array value to a list of strings, or raise."""
    if isinstance(value, str):
        return [value]
    if isinstance(value, list):
        items: list[str] = []
        for item in cast("list[object]", value):
            if not isinstance(item, str):
                raise ConfigError(f"devcontainer.json: '{what}' entries must be strings")
            items.append(item)
        return items
    raise ConfigError(f"devcontainer.json: '{what}' must be a string or array of strings")


def _normalize_compose_files(value: Any) -> tuple[str, ...]:
    """Normalize ``dockerComposeFile`` (str or list) to repo-root-relative paths.

    devcontainer.json declares the path(s) relative to the ``.devcontainer/`` folder; compose
    is invoked from the repo root, so each is re-rooted under ``.devcontainer/``.
    """
    raw = _as_str_list(value, "dockerComposeFile")
    if not raw:
        raise ConfigError("devcontainer.json: 'dockerComposeFile' is empty")
    return tuple(posixpath.normpath(posixpath.join(_DEVCONTAINER_DIR, item)) for item in raw)


def _normalize_post_create(value: Any) -> str | None:
    """Normalize ``postCreateCommand`` (str / list / absent) to a single shell string."""
    if value is None:
        return None
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return shlex.join(_as_str_list(value, "postCreateCommand"))
    raise ConfigError(
        "devcontainer.json: object form of 'postCreateCommand' is not yet supported; "
        "use a string or array"
    )


def facts_from_json(text: str, path: str) -> DevcontainerFacts:
    """Parse ``devcontainer.json`` text (JSONC) read from ``path`` into :class:`DevcontainerFacts`.

    The one parser of the data contract: ``read_facts`` and ``doctor``'s probe both use it,
    so the service and compose files ``doctor`` names are the ones ``start`` drives.
    """
    try:
        data = jsonc.loads(text)
    except ValueError as exc:
        raise ConfigError(f"invalid devcontainer.json at {path}: {exc}") from exc
    if "dockerComposeFile" not in data:
        raise ConfigError(f"{path}: missing 'dockerComposeFile' (billet drives compose)")
    for key in ("service", "workspaceFolder", "remoteUser"):
        if not isinstance(data.get(key), str):
            raise ConfigError(f"{path}: missing or non-string '{key}'")
    return DevcontainerFacts(
        service=data["service"],
        compose_files=_normalize_compose_files(data["dockerComposeFile"]),
        workspace_folder=data["workspaceFolder"],
        remote_user=data["remoteUser"],
        post_create_command=_normalize_post_create(data.get("postCreateCommand")),
    )


def compose_prelude(spec: WorkspaceSpec, remote: RemoteHost) -> str:
    """Shared remote-script header: fail-fast, cd into the repo, export billet's variables.

    Both ``BILLET_*`` variables the Workspace templates interpolate are exported before
    every ``docker compose`` invocation, so the repo's compose needs no ``.env`` on the Host.
    ``BILLET_CONTAINER_SSH_PORT`` lets the compose bind its sshd to billet's assigned
    loopback port (``127.0.0.1:${BILLET_CONTAINER_SSH_PORT:-2222}:22``, ADR-0003).
    ``BILLET_AUTHORIZED_KEYS`` names the Host admin user's ``authorized_keys`` so the
    container's sshd trusts the same key that opens the Host
    (``${BILLET_AUTHORIZED_KEYS:-./authorized_keys-stub}``). The admin user is the one this
    script already ssh's in as (``RemoteHost.admin_user``) — no new lookup. A shell export
    outranks compose's ``.env`` interpolation, which is what makes a stale ``.env`` left on
    a Host inert.
    """
    return (
        "set -euo pipefail\n"
        f"cd {shlex.quote(spec.repo_dir)}\n"
        f"export BILLET_CONTAINER_SSH_PORT={spec.container_ssh_port}\n"
        f"export BILLET_AUTHORIZED_KEYS={shlex.quote(_authorized_keys_path(remote))}\n"
    )


def running_ps_command(facts: DevcontainerFacts) -> str:
    """Build the ``docker compose ps`` that prints the running container id of the service.

    Scoped by service name (never by compose project). Since ADR-0017 each Workspace is its
    own compose project, so the scope is no longer what keeps Workspaces apart; it stays
    correct, and harmless, for a consumer whose compose file predates that rule and still
    shares a project with a sibling. ``is_running`` and ``doctor`` both use it.
    """
    return (
        f"docker compose {compose_file_flags(facts)} "
        f"ps --status running -q {shlex.quote(facts.service)}"
    )


def compose_file_flags(facts: DevcontainerFacts) -> str:
    """Build the ``-f <file>`` flags naming the service's compose files, each quoted.

    Every ``docker compose`` billet runs names its files through this, so ``doctor``'s ps
    and ``start``'s ``up`` read the same files.
    """
    return " ".join(f"-f {shlex.quote(path)}" for path in facts.compose_files)


def _authorized_keys_path(remote: RemoteHost) -> str:
    """Build the Host admin user's ``authorized_keys`` path — the file the container's sshd trusts."""
    return posixpath.join("/home", remote.admin_user, ".ssh", "authorized_keys")
