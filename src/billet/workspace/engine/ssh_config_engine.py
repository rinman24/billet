"""SshConfigEngine — render the tool-owned ``~/.ssh/config.d/billet.conf`` content.

Pure text rendering from :class:`SshConfigBlock` value objects. Two entry kinds per
Workspace:

- a **Host** entry for the VM (``host_alias`` -> public IP), emitted once per distinct host
  even when several Workspaces share it;
- a **container** entry reached via ``ProxyJump`` through the host, on the Workspace's
  loopback port, with ``ForwardAgent`` on and a stable ``HostKeyAlias`` (so the known-hosts
  entry is keyed on a collision-free name, not the volatile ``127.0.0.1:<port>``).

There is deliberately **no** ``RemoteCommand`` — an IDE's Remote-SSH needs a plain login
shell; the interactive tmux attach is ``billet connect``'s job, not the ssh-config's.
"""

from collections.abc import Sequence

from billet.contracts import SshConfigBlock

_HEADER = (
    "# Managed by billet — do not edit by hand.\n"
    "# Regenerate with `billet ssh-config`. billet fully owns this file."
)
_LOOPBACK = "127.0.0.1"


class SshConfigEngine:
    """Renders the ssh-config Include file for a set of Workspaces."""

    def render_conf(self, blocks: Sequence[SshConfigBlock]) -> str:
        """Render the full ``billet.conf`` body for ``blocks`` (ends with one newline)."""
        lines: list[str] = [_HEADER, ""]
        for block in self._distinct_hosts(blocks):
            lines += [
                f"Host {block.host_alias}",
                f"    HostName {block.host_ip}",
                f"    User {block.admin_user}",
                "",
            ]
        for block in blocks:
            lines += [
                f"Host {block.container_alias}",
                f"    ProxyJump {block.host_alias}",
                f"    HostName {_LOOPBACK}",
                f"    Port {block.container_port}",
                f"    User {block.container_user}",
                "    ForwardAgent yes",
                f"    HostKeyAlias {block.host_key_alias}",
                "",
            ]
        return "\n".join(lines).rstrip() + "\n"

    @staticmethod
    def _distinct_hosts(blocks: Sequence[SshConfigBlock]) -> list[SshConfigBlock]:
        """First-seen block per host alias (a shared Host is rendered once)."""
        seen: set[str] = set()
        distinct: list[SshConfigBlock] = []
        for block in blocks:
            if block.host_alias not in seen:
                seen.add(block.host_alias)
                distinct.append(block)
        return distinct
