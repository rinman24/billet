"""Tests for HostManager — plan composition per state and apply dispatch."""

import pytest

from billet.contracts import HostPowerState, HostSpec, HostStatus, StepKind
from billet.host.manager.host_manager import HostManager
from billet.shared.errors import HostOperationError
from tests.unit._fakes import FakeHostProvider, RecordingPlanObserver, make_host_spec

SPEC = make_host_spec()


def _status(state: HostPowerState, ip: str | None = None, raw: str = "") -> HostStatus:
    return HostStatus(power_state=state, public_ip=ip, raw_power=raw)


def test_plan_up_cold_create_is_billable_and_ordered() -> None:
    provider = FakeHostProvider(_status(HostPowerState.NOTEXIST))
    plan = HostManager(provider).plan_up(SPEC)
    assert [s.kind for s in plan.steps] == [
        StepKind.CREATE,
        StepKind.PIN_INBOUND,
        StepKind.WAIT_REACHABLE,
        StepKind.ENSURE_SUPPLY_CHAIN,
    ]
    assert plan.is_billable
    assert provider.calls == ["preflight", "status"]


def test_plan_up_resume_adopts_then_pins_then_starts() -> None:
    provider = FakeHostProvider(_status(HostPowerState.DEALLOCATED))
    plan = HostManager(provider).plan_up(SPEC)
    assert [s.kind for s in plan.steps] == [
        StepKind.ENSURE_TAGS,
        StepKind.PIN_INBOUND,
        StepKind.START,
        StepKind.WAIT_REACHABLE,
    ]
    assert not plan.is_billable


def test_plan_up_running_adopts_then_confirms_reachable() -> None:
    provider = FakeHostProvider(_status(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    plan = HostManager(provider).plan_up(SPEC)
    assert [s.kind for s in plan.steps] == [StepKind.ENSURE_TAGS, StepKind.WAIT_REACHABLE]


def test_plan_up_stopped_raises() -> None:
    provider = FakeHostProvider(_status(HostPowerState.STOPPED, raw="VM stopped"))
    with pytest.raises(HostOperationError, match="stopped"):
        HostManager(provider).plan_up(SPEC)


def test_plan_up_unexpected_state_raises() -> None:
    provider = FakeHostProvider(_status(HostPowerState.OTHER, raw="VM starting"))
    with pytest.raises(HostOperationError, match="unexpected state"):
        HostManager(provider).plan_up(SPEC)


def test_plan_stop_running_deallocates() -> None:
    provider = FakeHostProvider(_status(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    plan = HostManager(provider).plan_stop(SPEC)
    assert [s.kind for s in plan.steps] == [StepKind.DEALLOCATE]


def test_plan_stop_already_deallocated_is_empty() -> None:
    provider = FakeHostProvider(_status(HostPowerState.DEALLOCATED))
    plan = HostManager(provider).plan_stop(SPEC)
    assert plan.is_empty


def test_plan_stop_notexist_raises() -> None:
    provider = FakeHostProvider(_status(HostPowerState.NOTEXIST))
    with pytest.raises(HostOperationError, match="does not exist"):
        HostManager(provider).plan_stop(SPEC)


def test_plan_pin_ip_pins_only_without_reading_status() -> None:
    provider = FakeHostProvider(_status(HostPowerState.RUNNING, "1.2.3.4", "VM running"))
    plan = HostManager(provider).plan_pin_ip(SPEC)
    assert [s.kind for s in plan.steps] == [StepKind.PIN_INBOUND]
    assert provider.calls == ["preflight"]


def test_apply_dispatches_each_step_to_provider_in_order() -> None:
    provider = FakeHostProvider(_status(HostPowerState.NOTEXIST))
    manager = HostManager(provider)
    plan = manager.plan_up(SPEC)
    provider.calls.clear()
    manager.apply(plan, SPEC)
    assert provider.calls == [
        "create",
        "pin_inbound",
        "wait_until_reachable",
        "ensure_supply_chain",
    ]


def test_apply_resume_dispatches_adoption_first() -> None:
    provider = FakeHostProvider(_status(HostPowerState.DEALLOCATED))
    manager = HostManager(provider)
    plan = manager.plan_up(SPEC)
    provider.calls.clear()
    manager.apply(plan, SPEC)
    assert provider.calls == ["ensure_tags", "pin_inbound", "start", "wait_until_reachable"]


def test_apply_emits_started_then_succeeded_for_every_step_in_order() -> None:
    provider = FakeHostProvider(_status(HostPowerState.DEALLOCATED))
    manager = HostManager(provider)
    plan = manager.plan_up(SPEC)
    observer = RecordingPlanObserver()
    manager.apply(plan, SPEC, observer)
    expected: list[tuple[str, object]] = []
    for step in plan.steps:
        expected += [("started", step), ("succeeded", step)]
    assert observer.events == expected


def test_apply_emits_failed_reraises_and_runs_no_later_steps() -> None:
    class ExplodingProvider(FakeHostProvider):
        def start(self, spec: HostSpec) -> None:
            super().start(spec)
            raise HostOperationError("boom")

    provider = ExplodingProvider(_status(HostPowerState.DEALLOCATED))
    manager = HostManager(provider)
    plan = manager.plan_up(SPEC)  # ensure_tags, pin_inbound, start, wait_reachable
    provider.calls.clear()
    observer = RecordingPlanObserver()
    with pytest.raises(HostOperationError, match="boom"):
        manager.apply(plan, SPEC, observer)
    assert observer.events == [
        ("started", plan.steps[0]),
        ("succeeded", plan.steps[0]),
        ("started", plan.steps[1]),
        ("succeeded", plan.steps[1]),
        ("started", plan.steps[2]),
        ("failed", plan.steps[2]),
    ]
    assert provider.calls == ["ensure_tags", "pin_inbound", "start"]
