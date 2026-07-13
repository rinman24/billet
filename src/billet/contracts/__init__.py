"""Cross-layer data contracts and service Protocols — the dependency-inversion seam.

Everything references this layer; it references nothing domain-specific (only ``shared``).
See ADR-0001 for why the contracts live here rather than beside their subsystems.
"""

from billet.contracts.config import GlobalConfig
from billet.contracts.host import (
    HostPowerState,
    HostProvider,
    HostSpec,
    HostStatus,
    ProvisioningSpec,
)
from billet.contracts.metrics import (
    ContainerMetrics,
    CpuMetrics,
    DiskMetrics,
    HostMetrics,
    MemoryMetrics,
    MetricsAccess,
)
from billet.contracts.plan import NullPlanObserver, Plan, PlanObserver, PlanStep, StepKind
from billet.contracts.workspace import (
    ContainerAccess,
    DevcontainerFacts,
    RemoteHost,
    SourceAccess,
    SshConfigAccess,
    SshConfigBlock,
    WorkspacePlan,
    WorkspacePlanStep,
    WorkspaceSpec,
    WorkspaceStatus,
    WorkspaceStepKind,
)

__all__ = [
    "ContainerAccess",
    "ContainerMetrics",
    "CpuMetrics",
    "DevcontainerFacts",
    "DiskMetrics",
    "GlobalConfig",
    "HostMetrics",
    "HostPowerState",
    "HostProvider",
    "HostSpec",
    "HostStatus",
    "MemoryMetrics",
    "MetricsAccess",
    "NullPlanObserver",
    "Plan",
    "PlanObserver",
    "PlanStep",
    "ProvisioningSpec",
    "RemoteHost",
    "SourceAccess",
    "SshConfigAccess",
    "SshConfigBlock",
    "StepKind",
    "WorkspacePlan",
    "WorkspacePlanStep",
    "WorkspaceSpec",
    "WorkspaceStatus",
    "WorkspaceStepKind",
]
