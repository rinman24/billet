"""The Plan a manager builds and the client renders (dry-run) or executes.

Keeping dry-run/confirm at the client layer means the manager produces a Plan (pure data)
and ``access`` stays purely side-effecting — it never decides whether to run. See ADR-0001.
"""

from dataclasses import dataclass
from enum import Enum
from typing import Protocol

from billet.contracts.workspace import WorkspacePlanStep


class StepKind(Enum):
    """A host lifecycle operation the manager can schedule."""

    CREATE = "create"
    ENSURE_TAGS = "ensure_tags"
    PIN_INBOUND = "pin_inbound"
    START = "start"
    DEALLOCATE = "deallocate"
    WAIT_REACHABLE = "wait_reachable"
    ENSURE_SUPPLY_CHAIN = "ensure_supply_chain"


@dataclass(frozen=True, slots=True)
class PlanStep:
    """One scheduled operation plus a human-readable summary for dry-run rendering."""

    kind: StepKind
    summary: str
    billable: bool = False


@dataclass(frozen=True, slots=True)
class Plan:
    """An ordered set of steps for one host, with billable / empty introspection."""

    host_key: str
    steps: tuple[PlanStep, ...]

    @property
    def is_empty(self) -> bool:
        """True when there is nothing to do."""
        return not self.steps

    @property
    def is_billable(self) -> bool:
        """True when any step incurs cloud cost (a cold create)."""
        return any(step.billable for step in self.steps)


class PlanObserver(Protocol):
    """Receives step lifecycle events while a manager applies a plan.

    The observation seam of ADR-0001: managers emit semantic events (they still never
    print); the client decides how to render them. Host and Workspace plans carry sibling
    step types, so each event takes the union — one observer serves both managers.
    """

    def step_started(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Handle a step being dispatched (fires immediately before)."""
        ...

    def step_succeeded(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Handle a step's dispatch returning without raising."""
        ...

    def step_failed(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Handle a step's dispatch raising; the exception then propagates."""
        ...


class NullPlanObserver:
    """The silent default observer — every event is a no-op (headless applies)."""

    def step_started(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Ignore the event."""

    def step_succeeded(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Ignore the event."""

    def step_failed(self, step: PlanStep | WorkspacePlanStep) -> None:
        """Ignore the event."""
