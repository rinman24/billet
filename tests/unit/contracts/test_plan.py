"""Tests for the Plan data contract."""

from billet.contracts import Plan, PlanStep, StepKind


def test_empty_plan_is_empty_and_not_billable() -> None:
    plan = Plan(host_key="devbox", steps=())
    assert plan.is_empty
    assert not plan.is_billable


def test_plan_with_a_create_step_is_billable() -> None:
    plan = Plan(host_key="devbox", steps=(PlanStep(StepKind.CREATE, "create", billable=True),))
    assert not plan.is_empty
    assert plan.is_billable


def test_plan_without_billable_steps_is_not_billable() -> None:
    plan = Plan(host_key="devbox", steps=(PlanStep(StepKind.PIN_INBOUND, "pin"),))
    assert not plan.is_empty
    assert not plan.is_billable
