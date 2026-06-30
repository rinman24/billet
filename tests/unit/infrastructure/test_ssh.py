"""Tests for the ssh argv builder — TOFU host-key acceptance and the connect/clone flags."""

import pytest

from billet.infrastructure import ssh
from billet.infrastructure.process import CompletedProcess
from billet.shared.errors import HostOperationError
from tests.unit._fakes import FakeProcessRunner, completed


def test_basic_argv_targets_user_at_host_with_accept_new() -> None:
    argv = ssh.ssh_argv("azureuser", "1.2.3.4", "true")
    assert argv == [
        "ssh",
        "-o",
        "StrictHostKeyChecking=accept-new",
        "azureuser@1.2.3.4",
        "true",
    ]


def test_batch_and_connect_timeout_flags() -> None:
    argv = ssh.ssh_argv("u", "h", "true", connect_timeout=5, batch_mode=True)
    assert "ConnectTimeout=5" in argv
    assert "BatchMode=yes" in argv


def test_tty_adds_dash_t() -> None:
    argv = ssh.ssh_argv("u", "h", "cmd", tty=True)
    assert "-t" in argv
    assert "-A" not in argv


def test_forward_agent_adds_dash_capital_a() -> None:
    argv = ssh.ssh_argv("u", "h", forward_agent=True)
    assert "-A" in argv


def test_none_user_targets_a_bare_alias() -> None:
    # The connect path uses an ssh_config alias that already carries the user.
    argv = ssh.ssh_argv(None, "gswa-container", "cmd", tty=True)
    assert "gswa-container" in argv
    assert "@" not in " ".join(argv)


def test_operator_egress_ipv4_returns_trimmed_ip() -> None:
    runner = FakeProcessRunner(lambda _argv: completed(stdout="203.0.113.7\n"))
    assert ssh.operator_egress_ipv4(runner) == "203.0.113.7"


def test_operator_egress_ipv4_raises_when_empty() -> None:
    def handler(_argv: list[str]) -> CompletedProcess:
        return completed(stdout="")

    with pytest.raises(HostOperationError, match="egress"):
        ssh.operator_egress_ipv4(FakeProcessRunner(handler))
