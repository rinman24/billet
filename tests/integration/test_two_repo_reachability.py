"""Live two-repo reachability acceptance (Issue #3).

Reproduces the manual acceptance that let the lifted ``scripts/devbox/*.sh`` be removed:
two repos' Workspaces (``gswa-backend`` on port 2222, ``billet-smoke`` on 2223) run on the
one ``devbox`` Host, both reachable through a single SSH ``ProxyJump`` with distinct
``HostKeyAlias`` entries, while the Host keeps exactly one inbound Allow NSG rule.

Gating (this tree is a no-op in CI even though CI collects all of ``tests/``):

- module ``skipif(not BILLET_INTEGRATION)`` — auto-skips whenever the env var is unset;
- ``@pytest.mark.integration`` — documented marker for an explicit ``-m`` selection;
- ``conftest.py`` fixtures skip (never fail) on any missing prerequisite.

The tests **assume the environment is already up** and skip cleanly otherwise — they never
run ``billet start`` (its two-pass ``.env`` + first-clone host-key steps are the operator's
documented prereq, not the test's job).
"""

import json
import os
import subprocess
from typing import Any

import pytest

from tests.integration.conftest import GSWA, SMOKE, HostCfg, Workspace

pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(
        not os.environ.get("BILLET_INTEGRATION"),
        reason="live two-repo acceptance; set BILLET_INTEGRATION=1 to run",
    ),
]

_EXPECTED_PORTS = {2222, 2223}


def _run(cmd: list[str], timeout: int = 30) -> subprocess.CompletedProcess[str]:
    """Run ``cmd`` capturing text output, without raising on a non-zero exit."""
    return subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, check=False)


def _ssh_probe(alias: str) -> subprocess.CompletedProcess[str]:
    """Non-interactively probe reachability of ``alias`` through the ProxyJump."""
    return _run(["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10", alias, "true"])


def _nsg_inbound_allow_rules(host: HostCfg) -> list[dict[str, Any]]:
    """List the Host NSG's inbound Allow rules (skip if the query cannot run)."""
    result = _run(
        [
            "az",
            "network",
            "nsg",
            "rule",
            "list",
            "-g",
            host.resource_group,
            "--nsg-name",
            host.nsg_name,
            "-o",
            "json",
        ],
        timeout=60,
    )
    if result.returncode != 0:
        pytest.skip(f"`az network nsg rule list` failed: {result.stderr.strip()}")
    rules: list[dict[str, Any]] = json.loads(result.stdout)
    return [
        rule
        for rule in rules
        if str(rule.get("direction", "")).lower() == "inbound"
        and str(rule.get("access", "")).lower() == "allow"
    ]


def test_ssh_config_renders_both_containers_behind_one_host(
    billet_conf: str, workspaces: dict[str, Workspace]
) -> None:
    gswa = workspaces[GSWA]
    smoke = workspaces[SMOKE]

    # One shared ProxyJump Host, reached by both container entries.
    assert gswa.host_alias == smoke.host_alias
    assert billet_conf.count(f"Host {gswa.host_alias}\n") == 1
    assert billet_conf.count(f"ProxyJump {gswa.host_alias}") == 2

    # Two distinct container entries on the exact acceptance ports (2222/2223).
    assert f"Host {gswa.container_alias}" in billet_conf
    assert f"Host {smoke.container_alias}" in billet_conf
    assert {gswa.container_port, smoke.container_port} == _EXPECTED_PORTS
    assert f"Port {gswa.container_port}" in billet_conf
    assert f"Port {smoke.container_port}" in billet_conf

    # Each container carries its own collision-free HostKeyAlias.
    assert f"HostKeyAlias {gswa.container_alias}" in billet_conf
    assert f"HostKeyAlias {smoke.container_alias}" in billet_conf
    assert gswa.container_alias != smoke.container_alias

    # An IDE Remote-SSH needs a plain login shell — never a forced RemoteCommand.
    assert "RemoteCommand" not in billet_conf


@pytest.mark.parametrize("workspace_name", [GSWA, SMOKE])
def test_container_reachable_through_proxyjump(
    workspace_name: str, workspaces: dict[str, Workspace]
) -> None:
    alias = workspaces[workspace_name].container_alias
    result = _ssh_probe(alias)
    assert result.returncode == 0, (
        f"ssh to {alias!r} failed (rc={result.returncode}): {result.stderr.strip()}"
    )


def test_host_has_exactly_one_inbound_allow_rule(host_cfg: HostCfg) -> None:
    rules = _nsg_inbound_allow_rules(host_cfg)
    names = [str(rule.get("name")) for rule in rules]
    assert len(rules) == 1, f"expected exactly one inbound Allow rule, got {names}"


@pytest.mark.parametrize("workspace_name", [GSWA, SMOKE])
def test_billet_ls_reports_workspace_running(workspace_name: str) -> None:
    output = _run(["billet", "ls"]).stdout
    line = next((ln for ln in output.splitlines() if workspace_name in ln), None)
    assert line is not None, f"{workspace_name!r} not listed by `billet ls`:\n{output}"
    assert "running" in line
