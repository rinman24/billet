"""Cross-layer data contracts and service Protocols — the dependency-inversion seam.

Everything references this layer; it references nothing domain-specific (only ``shared``).
See ADR-0001 for why the contracts live here rather than beside their subsystems.
"""

from billet.contracts.config import GlobalConfig
from billet.contracts.host import HostPowerState, HostProvider, HostSpec, HostStatus
from billet.contracts.plan import Plan, PlanStep, StepKind
from billet.contracts.workspace import WorkspaceSpec

__all__ = [
    "GlobalConfig",
    "HostPowerState",
    "HostProvider",
    "HostSpec",
    "HostStatus",
    "Plan",
    "PlanStep",
    "StepKind",
    "WorkspaceSpec",
]
