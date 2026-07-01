"""Golden tests for SshConfigEngine — exact rendered ssh-config, ProxyJump/HostKeyAlias."""

from billet.contracts import SshConfigBlock
from billet.workspace.engine.ssh_config_engine import SshConfigEngine

_GSWA_BLOCK = SshConfigBlock(
    host_alias="gswa-devbox",
    host_ip="20.0.0.5",
    admin_user="azureuser",
    container_alias="gswa-container",
    container_port=2222,
    container_user="dev",
    host_key_alias="gswa-container",
)

_EXPECTED_SINGLE = """\
# Managed by billet — do not edit by hand.
# Regenerate with `billet ssh-config`. billet fully owns this file.

Host gswa-devbox
    HostName 20.0.0.5
    User azureuser

Host gswa-container
    ProxyJump gswa-devbox
    HostName 127.0.0.1
    Port 2222
    User dev
    ForwardAgent yes
    HostKeyAlias gswa-container
"""


def test_renders_single_workspace_exactly() -> None:
    assert SshConfigEngine().render_conf([_GSWA_BLOCK]) == _EXPECTED_SINGLE


def test_never_emits_a_remote_command() -> None:
    # IDE Remote-SSH needs a plain login shell; tmux is `billet connect`'s job.
    assert "RemoteCommand" not in SshConfigEngine().render_conf([_GSWA_BLOCK])


def test_shared_host_is_rendered_once_with_two_container_entries() -> None:
    second = SshConfigBlock(
        host_alias="gswa-devbox",
        host_ip="20.0.0.5",
        admin_user="azureuser",
        container_alias="other-container",
        container_port=2223,
        container_user="dev",
        host_key_alias="other-container",
    )
    conf = SshConfigEngine().render_conf([_GSWA_BLOCK, second])
    assert conf.count("Host gswa-devbox\n") == 1
    assert "Host gswa-container" in conf
    assert "Host other-container" in conf
    assert "Port 2222" in conf
    assert "Port 2223" in conf
    # Each container carries its own collision-free HostKeyAlias.
    assert "HostKeyAlias gswa-container" in conf
    assert "HostKeyAlias other-container" in conf


def test_distinct_hosts_each_get_a_host_entry() -> None:
    fleet = SshConfigBlock(
        host_alias="fleet-host",
        host_ip="20.0.0.9",
        admin_user="azureuser",
        container_alias="fleet-container",
        container_port=2222,
        container_user="dev",
        host_key_alias="fleet-container",
    )
    conf = SshConfigEngine().render_conf([_GSWA_BLOCK, fleet])
    assert "Host gswa-devbox" in conf
    assert "Host fleet-host" in conf
    assert "HostName 20.0.0.5" in conf
    assert "HostName 20.0.0.9" in conf
