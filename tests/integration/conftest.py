"""Prereq gate + shared fixtures for the live two-repo reachability acceptance.

These tests reproduce the Issue #3 manual acceptance: two repos' Workspaces
(``gswa-backend`` and ``billet-smoke``) running on the one ``devbox`` Host, both reachable
through a single SSH ``ProxyJump``. They run **only** on the operator Mac, which holds the
ssh-agent (VM admin key), ``~/.config/billet/config.toml``, ``az`` logged into
``genshift-energy``, and the billet-rendered ``~/.ssh/config.d/billet.conf``.

Every fixture here **skips** — never fails — when a prerequisite is missing, so an
under-provisioned environment reports as *skipped*, not *broken*. Combined with the
module-level ``skipif(not BILLET_INTEGRATION)`` in the test module, that keeps this tree a
no-op in CI even though CI collects all of ``tests/`` with no marker filter.

Operator prerequisites (bring the env up first; the tests never call ``billet start``):

- ``BILLET_INTEGRATION=1`` in the environment.
- ``az`` logged into subscription ``genshift-energy``.
- ssh-agent holding the ``gswa-devbox`` VM admin key.
- ``~/.config/billet/config.toml`` with ``[workspaces.gswa-backend]`` **and**
  ``[workspaces.billet-smoke]`` (and their ``[hosts.*]``).
- Both Workspaces already **started**, the operator IP pinned (``billet host pin-ip``), and
  ``~/.ssh/config.d/billet.conf`` rendered (``billet ssh-config``).
"""

from dataclasses import dataclass
from pathlib import Path
import shutil
import subprocess
import tomllib
from typing import Any

import pytest

from billet.shared.paths import resolve_config_path

#: Workspace names the acceptance expects to be registered and running.
GSWA = "gswa-backend"
SMOKE = "billet-smoke"


@dataclass(frozen=True)
class Workspace:
    """The config fields the acceptance asserts against for one Workspace."""

    name: str
    host: str
    host_alias: str
    container_alias: str
    container_port: int


@dataclass(frozen=True)
class HostCfg:
    """The Azure coordinates needed to inspect a Host's NSG."""

    resource_group: str
    nsg_name: str


def _parse_workspace(name: str, table: dict[str, Any]) -> Workspace:
    """Build a :class:`Workspace` from its ``[workspaces.<name>]`` table."""
    return Workspace(
        name=name,
        host=str(table["host"]),
        host_alias=str(table["host_alias"]),
        container_alias=str(table["container_alias"]),
        container_port=int(table["container_ssh_port"]),
    )


@pytest.fixture(scope="session", autouse=True)
def require_az_login() -> None:
    """Skip the whole tree unless ``az`` is present and logged in."""
    if shutil.which("az") is None:
        pytest.skip("`az` not on PATH; this acceptance runs on the operator Mac")
    token = subprocess.run(
        ["az", "account", "get-access-token", "--query", "expiresOn", "-o", "tsv"],
        capture_output=True,
        text=True,
        check=False,
    )
    if token.returncode != 0:
        pytest.skip("`az` is not logged in; run `az login` (subscription genshift-energy)")


@pytest.fixture(autouse=True)
def require_tools() -> None:
    """Skip a test unless ``billet`` and ``ssh`` are on PATH."""
    for tool in ("billet", "ssh"):
        if shutil.which(tool) is None:
            pytest.skip(f"`{tool}` not on PATH; this acceptance runs on the operator Mac")


@pytest.fixture(scope="session")
def config() -> dict[str, Any]:
    """Parse the operator ``config.toml`` (skip if absent)."""
    path = resolve_config_path(None)
    if not path.is_file():
        pytest.skip(f"no billet config at {path}; write config.toml with both workspaces")
    with path.open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture
def workspaces(config: dict[str, Any]) -> dict[str, Workspace]:
    """The two acceptance Workspaces from config (skip if either is unregistered)."""
    table: dict[str, Any] = config.get("workspaces", {})
    parsed: dict[str, Workspace] = {}
    for name in (GSWA, SMOKE):
        if name not in table:
            pytest.skip(f"workspace [{name}] not in config.toml; register it before running")
        parsed[name] = _parse_workspace(name, table[name])
    return parsed


@pytest.fixture
def host_cfg(config: dict[str, Any], workspaces: dict[str, Workspace]) -> HostCfg:
    """The Azure coordinates of the shared Host (skip if its ``[hosts.*]`` is absent)."""
    host_name = workspaces[GSWA].host
    hosts: dict[str, Any] = config.get("hosts", {})
    if host_name not in hosts:
        pytest.skip(f"host [{host_name}] not in config.toml")
    table: dict[str, Any] = hosts[host_name]
    # nsg_name defaults to "<vm_name>NSG" — billet's own default when the field is omitted.
    return HostCfg(
        resource_group=str(table["resource_group"]),
        nsg_name=str(table.get("nsg_name") or f"{table['vm_name']}NSG"),
    )


@pytest.fixture
def billet_conf() -> str:
    """The rendered ``~/.ssh/config.d/billet.conf`` body (skip if not yet written)."""
    path = Path.home() / ".ssh" / "config.d" / "billet.conf"
    if not path.is_file():
        pytest.skip(f"{path} not rendered; run `billet ssh-config` with both workspaces up")
    return path.read_text()
