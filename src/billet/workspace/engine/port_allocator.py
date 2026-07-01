"""PortAllocator — uniqueness + next-free policy for Workspace loopback ssh ports.

Each Workspace's in-container sshd is reached through a distinct loopback port on its Host
(``127.0.0.1:<port>``, behind the ``ProxyJump``). Ports must be unique *per host*; two
Workspaces on different hosts may reuse a port. This is pure policy — the engine never
inspects the live host (slice 6 wires the repo's compose to bind sshd to the assigned port).
"""

from collections.abc import Sequence

from billet.contracts import WorkspaceSpec
from billet.shared.errors import ConfigError

_DEFAULT_BASE_PORT = 2222


class PortAllocator:
    """Validates and assigns the loopback ssh port each Workspace binds on its Host."""

    def __init__(self, base_port: int = _DEFAULT_BASE_PORT) -> None:
        self._base_port = base_port

    def assert_unique(self, specs: Sequence[WorkspaceSpec]) -> None:
        """Raise :class:`ConfigError` if two Workspaces on the same Host share a port."""
        seen: dict[tuple[str, int], str] = {}
        for spec in specs:
            key = (spec.host, spec.container_ssh_port)
            if key in seen:
                raise ConfigError(
                    f"port collision on host '{spec.host}': workspaces '{seen[key]}' and "
                    f"'{spec.key}' both bind loopback port {spec.container_ssh_port}"
                )
            seen[key] = spec.key

    def is_free(self, port: int, specs: Sequence[WorkspaceSpec], host: str) -> bool:
        """Return whether ``port`` is unused by any Workspace on ``host``."""
        return port not in self._used_ports(specs, host)

    def next_free(self, specs: Sequence[WorkspaceSpec], host: str) -> int:
        """Return the lowest free loopback port on ``host`` at or above the base port."""
        used = self._used_ports(specs, host)
        port = self._base_port
        while port in used:
            port += 1
        return port

    @staticmethod
    def _used_ports(specs: Sequence[WorkspaceSpec], host: str) -> set[int]:
        return {spec.container_ssh_port for spec in specs if spec.host == host}
