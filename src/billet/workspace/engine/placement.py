"""HostPlacementPolicy — the Workspace→Host placement invariant (ADR-0004).

A Workspace may only be placed on a Host that manages Workspaces
(``HostSpec.manages_workspaces``). A Host that opts out (e.g. the fleet-host, whose runtime
is squadra's, not a devcontainer billet clones) carries no Workspaces — billet manages only
its VM lifecycle via ``billet host … --host <key>``.

This is pure policy — a predicate over the ``HostSpec`` contract DTO, no I/O — the same shape
as :class:`~billet.workspace.engine.port_allocator.PortAllocator`. The enforcement *semantics*
(the raise) live here; command verbs trigger them through the manager. ``billet ls`` (a query)
reads the flag for its projection instead of raising (ADR-0004 §2).
"""

from billet.contracts import HostSpec
from billet.shared.errors import ConfigError


class HostPlacementPolicy:
    """Validates that a Host may carry Workspaces."""

    def assert_manages_workspaces(self, host: HostSpec) -> None:
        """Raise :class:`ConfigError` if ``host`` does not manage Workspaces."""
        if not host.manages_workspaces:
            raise ConfigError(
                f"host '{host.key}' has manages_workspaces = false; it carries no Workspaces. "
                f"Use `billet host <verb> --host {host.key}` to manage its VM lifecycle."
            )
