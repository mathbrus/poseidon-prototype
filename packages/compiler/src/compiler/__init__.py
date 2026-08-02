"""The compiler (overview §4.1, plan step 9).

`(blueprint, role_catalog, registry_snapshot) → deployment_plan`, pure: no
cluster mutation, no secrets in agent specs, no OASF/Directory dependency.
Every enforcement projection — CRs, Rego bundles, the infrastructure policy
IR, crew artifacts, budgets, gate config — comes from this one function, so
a governance mistake fails at compile instead of at the first tool call.
"""

from compiler.compile import PLATFORM_SCOPE, compile_blueprint
from compiler.diff import PlanDiff, diff_plan
from compiler.errors import CompileError
from compiler.plan import (
    AgentInstanceCR,
    BudgetRegistration,
    CompanyCR,
    CrewMemberSpec,
    DeploymentPlan,
    InfraIR,
    RegoBundle,
    WorkflowConfig,
)
from compiler.snapshot import EMPTY, InstanceSnapshot, RegistrySnapshot

__all__ = [
    "AgentInstanceCR",
    "BudgetRegistration",
    "CompanyCR",
    "CompileError",
    "CrewMemberSpec",
    "DeploymentPlan",
    "EMPTY",
    "InfraIR",
    "InstanceSnapshot",
    "PLATFORM_SCOPE",
    "PlanDiff",
    "RegistrySnapshot",
    "RegoBundle",
    "WorkflowConfig",
    "compile_blueprint",
    "diff_plan",
]
