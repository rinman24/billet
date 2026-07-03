"""HostManager — orchestrates host lifecycle behind the ``HostProvider`` seam.

The manager *plans* (reads live state, composes an ordered :class:`Plan`) and *applies*
(dispatches each step to the provider). It never prompts or prints — dry-run and the
billable-create confirm live in the client. See ADR-0001.
"""

from billet.contracts import (
    HostMetrics,
    HostPowerState,
    HostProvider,
    HostSpec,
    HostStatus,
    MetricsAccess,
    Plan,
    PlanStep,
    RemoteHost,
    StepKind,
)
from billet.shared.errors import HostOperationError


def _adopt_step(spec: HostSpec) -> PlanStep:
    return PlanStep(StepKind.ENSURE_TAGS, f"adopt VM {spec.vm_name} (tag managed-by=billet)")


def _pin_step(spec: HostSpec) -> PlanStep:
    return PlanStep(StepKind.PIN_INBOUND, f"pin inbound SSH on {spec.nsg_name} to operator /32")


def _wait_step(spec: HostSpec, *, verb: str = "wait for") -> PlanStep:
    return PlanStep(StepKind.WAIT_REACHABLE, f"{verb} SSH on {spec.vm_name}")


class HostManager:
    """Plans and applies the lifecycle of one Host through an injected provider."""

    def __init__(self, provider: HostProvider) -> None:
        self._provider = provider

    def plan_up(self, spec: HostSpec) -> Plan:
        """Build the plan to bring a host up (cold provision or resume, auto-detected)."""
        self._provider.preflight()
        state = self._provider.status(spec).power_state
        if state is HostPowerState.NOTEXIST:
            return Plan(
                host_key=spec.key,
                steps=(
                    PlanStep(
                        StepKind.CREATE,
                        f"create resource group + VM {spec.vm_name} ({spec.vm_size}, BILLABLE)",
                        billable=True,
                    ),
                    _pin_step(spec),
                    _wait_step(spec),
                    PlanStep(StepKind.ENSURE_SUPPLY_CHAIN, "install Docker (base supply chain)"),
                ),
            )
        if state is HostPowerState.DEALLOCATED:
            return Plan(
                host_key=spec.key,
                steps=(
                    _adopt_step(spec),
                    _pin_step(spec),
                    PlanStep(StepKind.START, f"start VM {spec.vm_name}"),
                    _wait_step(spec),
                ),
            )
        if state is HostPowerState.RUNNING:
            return Plan(
                host_key=spec.key,
                steps=(_adopt_step(spec), _wait_step(spec, verb="confirm")),
            )
        if state is HostPowerState.STOPPED:
            raise HostOperationError(
                f"VM {spec.vm_name} is 'stopped' (not deallocated). "
                "Start it manually or deallocate it, then retry."
            )
        raise HostOperationError(f"VM {spec.vm_name} is in an unexpected state; cannot plan an up.")

    def plan_stop(self, spec: HostSpec) -> Plan:
        """Build the plan to deallocate a host (empty if already deallocated)."""
        self._provider.preflight()
        state = self._provider.status(spec).power_state
        if state is HostPowerState.NOTEXIST:
            raise HostOperationError(f"VM {spec.vm_name} does not exist — nothing to stop.")
        if state is HostPowerState.DEALLOCATED:
            return Plan(host_key=spec.key, steps=())
        return Plan(
            host_key=spec.key,
            steps=(
                PlanStep(
                    StepKind.DEALLOCATE,
                    f"deallocate VM {spec.vm_name} (stops compute billing)",
                ),
            ),
        )

    def plan_pin_ip(self, spec: HostSpec) -> Plan:
        """Build the plan to re-pin the inbound SSH rule to the operator's current /32."""
        self._provider.preflight()
        return Plan(host_key=spec.key, steps=(_pin_step(spec),))

    def read_metrics(
        self, spec: HostSpec, metrics: MetricsAccess
    ) -> tuple[HostStatus, HostMetrics]:
        """Probe a running host's live usage (a query — no plan, nothing is changed).

        Raises
        ------
        HostOperationError
            If the host is not running (there is nothing to probe).
        """
        self._provider.preflight()
        status = self._provider.status(spec)
        if status.power_state is not HostPowerState.RUNNING or status.public_ip is None:
            raise HostOperationError(
                f"VM {spec.vm_name} is not running ({status.raw_power or 'not found'}) — "
                "bring it up with `billet host up` first."
            )
        remote = RemoteHost(admin_user=spec.admin_user, ip=status.public_ip)
        return status, metrics.read(remote)

    def apply(self, plan: Plan, spec: HostSpec) -> None:
        """Execute each step in order against the provider."""
        for step in plan.steps:
            self._dispatch(step.kind, spec)

    def _dispatch(self, kind: StepKind, spec: HostSpec) -> None:
        if kind is StepKind.CREATE:
            self._provider.create(spec)
        elif kind is StepKind.ENSURE_TAGS:
            self._provider.ensure_tags(spec)
        elif kind is StepKind.PIN_INBOUND:
            self._provider.pin_inbound(spec)
        elif kind is StepKind.START:
            self._provider.start(spec)
        elif kind is StepKind.DEALLOCATE:
            self._provider.deallocate(spec)
        elif kind is StepKind.WAIT_REACHABLE:
            self._provider.wait_until_reachable(spec)
        elif kind is StepKind.ENSURE_SUPPLY_CHAIN:
            self._provider.ensure_supply_chain(spec)
