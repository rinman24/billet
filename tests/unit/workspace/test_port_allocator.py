"""Tests for PortAllocator — per-host uniqueness, freedom checks, and next-free."""

import pytest

from billet.shared.errors import ConfigError
from billet.workspace.engine.port_allocator import PortAllocator
from tests.unit._fakes import make_workspace_spec


def test_assert_unique_passes_for_distinct_ports() -> None:
    specs = [
        make_workspace_spec(key="a", host="devbox", container_ssh_port=2222),
        make_workspace_spec(key="b", host="devbox", container_ssh_port=2223),
    ]
    PortAllocator().assert_unique(specs)  # no raise


def test_assert_unique_raises_on_same_host_collision() -> None:
    specs = [
        make_workspace_spec(key="a", host="devbox", container_ssh_port=2222),
        make_workspace_spec(key="b", host="devbox", container_ssh_port=2222),
    ]
    with pytest.raises(ConfigError, match="port collision on host 'devbox'"):
        PortAllocator().assert_unique(specs)


def test_assert_unique_allows_same_port_on_different_hosts() -> None:
    specs = [
        make_workspace_spec(key="a", host="devbox", container_ssh_port=2222),
        make_workspace_spec(key="b", host="fleet", container_ssh_port=2222),
    ]
    PortAllocator().assert_unique(specs)  # no raise


def test_is_free_reflects_usage_on_the_named_host() -> None:
    specs = [make_workspace_spec(key="a", host="devbox", container_ssh_port=2222)]
    allocator = PortAllocator()
    assert allocator.is_free(2222, specs, "devbox") is False
    assert allocator.is_free(2222, specs, "fleet") is True
    assert allocator.is_free(2223, specs, "devbox") is True


def test_next_free_returns_base_when_unused() -> None:
    assert PortAllocator().next_free([], "devbox") == 2222


def test_next_free_skips_used_ports() -> None:
    specs = [
        make_workspace_spec(key="a", host="devbox", container_ssh_port=2222),
        make_workspace_spec(key="b", host="devbox", container_ssh_port=2223),
    ]
    assert PortAllocator().next_free(specs, "devbox") == 2224


def test_next_free_ignores_other_hosts() -> None:
    specs = [make_workspace_spec(key="a", host="fleet", container_ssh_port=2222)]
    assert PortAllocator().next_free(specs, "devbox") == 2222
