"""Tests for AzureVmHostProvider — argv spies, state mapping, and security invariants."""

from collections.abc import Callable

import pytest

from billet.access.host.azure_vm_provider import AzureVmHostProvider
from billet.contracts import HostPowerState
from billet.infrastructure.process import CompletedProcess
from billet.shared.errors import AzLoginRequired, HostOperationError
from tests.unit._fakes import FakeProcessRunner, completed, make_host_spec

SPEC = make_host_spec()

Handler = Callable[[list[str]], CompletedProcess]


def _provider(
    handler: Handler, *, ssh_attempts: int = 30
) -> tuple[AzureVmHostProvider, FakeProcessRunner]:
    runner = FakeProcessRunner(handler)
    provider = AzureVmHostProvider(
        runner,
        subscription_id="sub-123",
        ssh_attempts=ssh_attempts,
        sleep=lambda _seconds: None,
    )
    return provider, runner


# --- status mapping ----------------------------------------------------------------


def test_status_notexist_when_vm_show_fails() -> None:
    provider, _ = _provider(lambda _argv: completed(returncode=1, stderr="not found"))
    status = provider.status(SPEC)
    assert status.power_state is HostPowerState.NOTEXIST
    assert status.public_ip is None


def test_status_running_includes_public_ip() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "powerState" in argv:
            return completed(stdout="VM running\n")
        if "publicIps" in argv:
            return completed(stdout="20.0.0.5\n")
        return completed()

    provider, _ = _provider(handler)
    status = provider.status(SPEC)
    assert status.power_state is HostPowerState.RUNNING
    assert status.public_ip == "20.0.0.5"


def test_status_deallocated_has_no_ip() -> None:
    provider, _ = _provider(lambda _argv: completed(stdout="VM deallocated\n"))
    status = provider.status(SPEC)
    assert status.power_state is HostPowerState.DEALLOCATED
    assert status.public_ip is None


def test_status_unknown_state_preserves_raw_power() -> None:
    provider, _ = _provider(lambda _argv: completed(stdout="VM starting\n"))
    status = provider.status(SPEC)
    assert status.power_state is HostPowerState.OTHER
    assert status.raw_power == "VM starting"


# --- mutating control-plane ops ----------------------------------------------------


def test_create_tags_vm_as_billet_managed() -> None:
    provider, runner = _provider(lambda _argv: completed())
    provider.create(SPEC)
    cmds = runner.commands()
    assert any(c.startswith("az group create") for c in cmds)
    create = next(c for c in cmds if "az vm create" in c)
    assert "managed-by=billet" in create
    assert "billet-host=devbox" in create
    assert "--generate-ssh-keys" in create


def test_start_issues_az_vm_start() -> None:
    provider, runner = _provider(lambda _argv: completed())
    provider.start(SPEC)
    assert any("az vm start" in c for c in runner.commands())


def test_deallocate_issues_az_vm_deallocate() -> None:
    provider, runner = _provider(lambda _argv: completed())
    provider.deallocate(SPEC)
    assert any("az vm deallocate" in c for c in runner.commands())


def test_ensure_tags_merges_billet_ownership_tags() -> None:
    provider, runner = _provider(lambda _argv: completed())
    provider.ensure_tags(SPEC)
    update = next(c for c in runner.commands() if "az vm update" in c)
    assert "tags.managed-by=billet" in update
    assert "tags.billet-host=devbox" in update


# --- security invariant: inbound is always a single /32 ----------------------------


def test_pin_inbound_uses_operator_ip_slash_32_only() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if argv and argv[0] == "curl":
            return completed(stdout="203.0.113.7\n")
        return completed()

    provider, runner = _provider(handler)
    cidr = provider.pin_inbound(SPEC)
    assert cidr == "203.0.113.7/32"
    nsg = next(c for c in runner.commands() if "nsg rule update" in c)
    assert "--source-address-prefixes 203.0.113.7/32" in nsg
    assert "0.0.0.0/0" not in nsg


def test_pin_inbound_raises_when_operator_ip_unknown() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if argv and argv[0] == "curl":
            return completed(returncode=0, stdout="")
        return completed()

    provider, _ = _provider(handler)
    with pytest.raises(HostOperationError, match="operator egress"):
        provider.pin_inbound(SPEC)


# --- preflight (az auth + subscription pin) ----------------------------------------


def test_preflight_raises_when_not_logged_in() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "get-access-token" in argv:
            return completed(returncode=1)
        return completed()

    provider, _ = _provider(handler)
    with pytest.raises(AzLoginRequired):
        provider.preflight()


def test_preflight_pins_and_verifies_subscription() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "get-access-token" in argv:
            return completed(stdout="2030-01-01")
        if argv[:3] == ["az", "account", "show"]:
            return completed(stdout="sub-123\n")
        return completed()

    provider, runner = _provider(handler)
    provider.preflight()
    assert any("az account set --subscription sub-123" in c for c in runner.commands())


def test_preflight_raises_on_subscription_mismatch() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "get-access-token" in argv:
            return completed(stdout="2030")
        if argv[:3] == ["az", "account", "show"]:
            return completed(stdout="other-sub\n")
        return completed()

    provider, _ = _provider(handler)
    with pytest.raises(HostOperationError, match="failed to pin subscription"):
        provider.preflight()


# --- reachability + supply chain ---------------------------------------------------


def test_wait_until_reachable_returns_on_first_success() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "publicIps" in argv:
            return completed(stdout="20.0.0.5\n")
        return completed()  # ssh true -> rc 0

    provider, runner = _provider(handler, ssh_attempts=3)
    provider.wait_until_reachable(SPEC)
    ssh_calls = [c for c in runner.commands() if c.startswith("ssh ")]
    assert len(ssh_calls) == 1
    assert ssh_calls[0].endswith("true")


def test_wait_until_reachable_raises_after_exhausting_attempts() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "publicIps" in argv:
            return completed(stdout="20.0.0.5\n")
        if argv and argv[0] == "ssh":
            return completed(returncode=255)
        return completed()

    provider, runner = _provider(handler, ssh_attempts=3)
    with pytest.raises(HostOperationError, match="SSH did not come up"):
        provider.wait_until_reachable(SPEC)
    assert len([c for c in runner.commands() if c.startswith("ssh ")]) == 3


def test_ensure_supply_chain_pipes_idempotent_docker_script_over_ssh() -> None:
    def handler(argv: list[str]) -> CompletedProcess:
        if "publicIps" in argv:
            return completed(stdout="20.0.0.5\n")
        return completed()

    provider, runner = _provider(handler)
    provider.ensure_supply_chain(SPEC)
    ssh_calls = [c for c in runner.commands() if c.startswith("ssh ")]
    assert ssh_calls and ssh_calls[-1].endswith("bash -se")
    script = runner.inputs[-1]
    assert script is not None
    assert "command -v docker" in script
    assert "apt-get install -y" in script
    assert "signed-by=/etc/apt/keyrings/docker.gpg" in script


def test_ensure_supply_chain_raises_without_public_ip() -> None:
    provider, _ = _provider(lambda _argv: completed(stdout=""))
    with pytest.raises(HostOperationError, match="no public IP"):
        provider.ensure_supply_chain(SPEC)
