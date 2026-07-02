"""Tests for HostPlacementPolicy — the Workspace→Host placement invariant (ADR-0004)."""

import pytest

from billet.shared.errors import ConfigError
from billet.workspace.engine.placement import HostPlacementPolicy
from tests.unit._fakes import make_host_spec


def test_assert_manages_workspaces_passes_for_managing_host() -> None:
    HostPlacementPolicy().assert_manages_workspaces(make_host_spec(manages_workspaces=True))


def test_assert_manages_workspaces_raises_for_non_managing_host() -> None:
    host = make_host_spec(key="fleet", manages_workspaces=False)
    with pytest.raises(ConfigError, match="host 'fleet' has manages_workspaces = false"):
        HostPlacementPolicy().assert_manages_workspaces(host)


def test_error_names_the_host_lifecycle_escape_hatch() -> None:
    host = make_host_spec(key="fleet", manages_workspaces=False)
    with pytest.raises(ConfigError, match=r"billet host .* --host fleet"):
        HostPlacementPolicy().assert_manages_workspaces(host)
