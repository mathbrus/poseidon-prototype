"""The deployment plan: everything a blueprint compiles into.

One source of truth, several emissions. The plan is data — the
operator applies it, the compiler never touches a cluster.
Every type is a plain dataclass with a deterministic `as_dict()` so plans
are diffable and golden-testable.
"""

from dataclasses import asdict, dataclass
from typing import Any


@dataclass(frozen=True)
class CompanyCR:
    """The `Company` custom resource (see crds/)."""

    name: str
    namespace: str
    mission: str
    stage: str | None = None


@dataclass(frozen=True)
class AgentInstanceCR:
    """One standing agent. Never carries a secret: agent specs are public,
    credentials arrive at runtime as projected tokens."""

    agent_id: str
    company: str
    standing_name: str
    role: str
    model_tier: str
    tools: tuple[str, ...]
    may_spawn: tuple[str, ...] = ()
    max_concurrent_spawns: int | None = None


@dataclass(frozen=True)
class EphemeralAgentSpec:
    """Crew artifact: what an ephemeral agent is allowed to be. Not an
    instance — agents exist only inside a run."""

    crew: str
    role: str
    tools: tuple[str, ...]


@dataclass(frozen=True)
class WorkflowConfig:
    name: str
    enabled: bool
    conductor: str
    crew: str | None
    # Gate objects as the blueprint wrote them: `{resolved_by, delegate?}`.
    # A gate nobody declares is absent, and absent means the workflow does
    # not stop there.
    gates: dict[str, Any]
    max_verify_retries: int


@dataclass(frozen=True)
class BudgetRegistration:
    per_run_usd: float
    # How long a run stopped at the cap waits for a human to raise it.
    pause_minutes: int


@dataclass(frozen=True)
class HeartbeatConfig:
    """The standing heartbeat's declared authority. Absent (a plan without
    one) means what it always meant: runs start only when a human starts
    one."""

    conductor: str
    reconcile_minutes: int
    max_concurrent_runs: int
    # Runs the heartbeat may start on one issue or PR before it stops
    # offering it; concluded runs consume the item regardless.
    max_attempts: int
    may_start: tuple[str, ...]
    daily_cap_usd: float


@dataclass(frozen=True)
class SandboxConstraints:
    """The Job-spec projection of the infra IR: what `provision_sandbox`
    passes as `SandboxPolicy`."""

    image: str
    workspace_dir: str
    cpu: str
    memory: str
    disk: str
    deadline_s: int
    egress_allow: tuple[str, ...]
    # The blueprint's test command — what the platform verify gate runs.
    test_command: str


@dataclass(frozen=True)
class NetworkPolicyProjection:
    """The namespace envelope the operator owns: sandboxes answer the
    gateway and reach only the egress proxy."""

    namespace: str
    sandbox_ingress_from: tuple[str, ...]
    sandbox_egress_to: tuple[str, ...]
    default_deny: bool = True


@dataclass(frozen=True)
class InfraIR:
    """One neutral IR with several projections. Hand-authoring any
    projection is a bug — they all come from `stack` + role + policies."""

    sandbox: SandboxConstraints
    network_policy: NetworkPolicyProjection
    egress_proxy_allowlist: tuple[str, ...]


@dataclass(frozen=True)
class RegoBundle:
    """Compiled policy: a bundle is a set of Rego files installed together
    and served over the standard OPA bundle API; nothing is pushed to OPA
    at runtime."""

    files: dict[str, str]


@dataclass(frozen=True)
class DeploymentPlan:
    company: CompanyCR
    instances: tuple[AgentInstanceCR, ...]
    ephemeral_agents: tuple[EphemeralAgentSpec, ...]
    workflows: tuple[WorkflowConfig, ...]
    budgets: BudgetRegistration
    infra: InfraIR
    bundle: RegoBundle
    # The orchestrator's compiler-owned scope, its own bundle:
    # per-company packages keep company roots disjoint, and the platform
    # rules — identical from every blueprint — live under poseidon.platform.
    platform_bundle: RegoBundle
    heartbeat: HeartbeatConfig | None = None
    # The contract tree, projected into the applied record: the review
    # workflow composes the contract-verify baseline from these paths.
    # None when the blueprint declares no contract tree.
    contract_tests: dict[str, Any] | None = None

    def as_dict(self) -> dict[str, Any]:
        """Deterministic and JSON-shaped. The diff and the golden tests both
        read this, so ordering is fixed by construction, never by chance,
        and tuples become lists — otherwise a plan would not equal itself
        after a round trip through a file."""
        return _jsonable(
            {
                "company": asdict(self.company),
                "instances": [asdict(i) for i in self.instances],
                "ephemeral_agents": [asdict(m) for m in self.ephemeral_agents],
                "workflows": [asdict(w) for w in self.workflows],
                "budgets": asdict(self.budgets),
                "heartbeat": asdict(self.heartbeat) if self.heartbeat else None,
                "contract_tests": self.contract_tests,
                "infra": asdict(self.infra),
                "bundle": {"files": dict(sorted(self.bundle.files.items()))},
                "platform_bundle": {
                    "files": dict(sorted(self.platform_bundle.files.items()))
                },
            }
        )


def _jsonable(value: Any) -> Any:
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    return value
