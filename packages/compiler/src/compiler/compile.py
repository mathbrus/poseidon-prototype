"""The compiler: `(blueprint, role_catalog, registry_snapshot) → plan`.

A pure function. It reads nothing but its arguments, writes nothing,
and touches no cluster: apply is the operator's job, and keeping the
mutation in exactly one place is what makes "the blueprint is the source
of truth" checkable rather than aspirational.

Secrets never enter a plan. Agent specs carry a role, a tier and a tool
scope; credentials arrive at runtime as projected tokens.
"""

from typing import Any

from compiler.checks import check, referenced_crews
from compiler.errors import CompileError
from compiler.infra import build_infra
from compiler.plan import (
    AgentInstanceCR,
    BudgetRegistration,
    CompanyCR,
    DeploymentPlan,
    EphemeralAgentSpec,
    HeartbeatConfig,
    RegoBundle,
    Repo,
    WorkflowConfig,
)
from compiler.rego import build_bundle, build_platform_bundle
from compiler.snapshot import EMPTY, RegistrySnapshot
from schema.catalog import CREW_TEMPLATES, ROLES, CrewTemplate, Role
from schema.tools import PLATFORM_TOOLS, REPO_TOOLS

# What the orchestrator's own identity may call. Not blueprint-configurable:
# a company that could grant itself the platform lifecycle would be able to
# merge its own pull requests. The two /admin entries are the
# gateway's admin routes, policed under their path as the tool name —
# both must be here or sandbox registration and teardown die.
PLATFORM_SCOPE = sorted(
    {
        *PLATFORM_TOOLS,
        *REPO_TOOLS,
        "sandbox.run_tests",
        # The review workflow resolves the PR's head ref before provisioning
        # the review sandbox from it — a read, but the platform's own,
        # like repo.archive.
        "git.read",
        # The implement workflow resolves an issue-driven run's ticket
        # before dispatch — the same platform-read posture as git.read.
        "issues.read",
        # The review workflow's rework loop files a failed policy-gated
        # review's findings back into the backlog as a ticket — a platform
        # write, attributed to the review run, that the conductor then
        # consumes like any other issue.
        "issues.create",
        # The oracle-edit warning: when a PR's diff touches the contract
        # tree, the platform posts the model-free deletion facts on the PR
        # so the human resolving the merge signal reads them first — a
        # gutted spec must be impossible to approve silently.
        "git.comment",
        # The attempt-exhaustion notice: when the heartbeat's last allowed
        # run on a PR or issue aborts, the platform says so on the item
        # itself (git.comment above for a PR, issues.comment for a ticket),
        # so the abandonment is never silent.
        "issues.comment",
        "/admin/sandboxes",
        "/admin/sandboxes.delete",
    }
)


def compile_blueprint(
    blueprint: dict[str, Any],
    role_catalog: dict[str, Role] | None = None,
    crew_catalog: dict[str, CrewTemplate] | None = None,
    snapshot: RegistrySnapshot = EMPTY,
) -> DeploymentPlan:
    roles = role_catalog or ROLES
    crews_catalog = crew_catalog or CREW_TEMPLATES

    problems = check(blueprint, roles, crews_catalog)
    if problems:
        raise CompileError(problems)

    company_section = blueprint["company"]
    company = company_section["name"]
    namespace = company
    repo = Repo(**blueprint["stack"]["repo"])
    company_cr = CompanyCR(
        name=company, namespace=namespace, mission=company_section["mission"], repo=repo
    )

    scopes = blueprint.get("tools") or {}
    instances = _instances(blueprint, roles, scopes, company, snapshot, repo)
    ephemeral_agents = _ephemeral_agents(blueprint, roles, crews_catalog, scopes)
    workflows = _workflows(blueprint)
    budgets = _budgets(blueprint)
    heartbeat = _heartbeat(blueprint)
    infra = build_infra(blueprint, namespace)
    bundle = RegoBundle(
        files=build_bundle(
            company=company,
            instances=list(instances),
            ephemeral_agents=list(ephemeral_agents),
            policies=blueprint.get("policies") or {},
            repo_url=repo.url,
            heartbeat=heartbeat,
            merge_delegation=_merge_delegation(workflows),
        )
    )
    return DeploymentPlan(
        company=company_cr,
        instances=instances,
        ephemeral_agents=ephemeral_agents,
        workflows=workflows,
        budgets=budgets,
        infra=infra,
        bundle=bundle,
        platform_bundle=RegoBundle(files=build_platform_bundle(PLATFORM_SCOPE)),
        heartbeat=heartbeat,
        contract_tests=(blueprint.get("policies") or {}).get("contract_tests"),
    )


def _scope(role: Role, scopes: dict[str, Any]) -> tuple[str, ...]:
    """The role's compiled tools: the blueprint's `tools.<role>.allow`
    narrowed against the catalog, or the catalog's list when the blueprint
    declares none. The blueprint narrows the catalog, never widens it — a
    widening scope is a checks.py error, and even past that, nothing wider
    than the catalog's list may reach a compiled bundle."""
    declared = set((scopes.get(role.name) or {}).get("allow") or role.tools)
    return tuple(sorted(declared & set(role.tools)))


def _instances(
    blueprint: dict[str, Any],
    roles: dict[str, Role],
    scopes: dict[str, Any],
    company: str,
    snapshot: RegistrySnapshot,
    repo: Repo,
) -> tuple[AgentInstanceCR, ...]:
    """Standing agents only. A `count: 0` placeholder produces no instance —
    that is the whole point of declaring one."""
    mission = blueprint["company"]["mission"]
    instances = []
    for standing_name, hire in sorted((blueprint.get("standing") or {}).items()):
        count = hire.get("count", 0)
        role = roles[hire["role"]]
        # Spawn authority narrows like tools do — checks.py errors on a
        # widening attempt; the intersection is the belt under that brace.
        may_spawn = tuple(
            sorted(
                set((hire.get("may_spawn") or {}).get("crew") or []) & set(role.may_spawn)
            )
        )
        # A hire is one instance. A converged instance keeps the id policy
        # and audit already know it by; a new hire is `<standing_name>-1`.
        live = snapshot.instances_for(company, standing_name)
        agent_ids = ([live[0].agent_id] if live else [f"{standing_name}-1"])[:count]
        for agent_id in agent_ids:
            instances.append(
                AgentInstanceCR(
                    agent_id=agent_id,
                    company=company,
                    standing_name=standing_name,
                    role=role.name,
                    tools=_scope(role, scopes),
                    repo=repo,
                    mission=mission,
                    may_spawn=may_spawn,
                    max_concurrent_spawns=(hire.get("may_spawn") or {}).get(
                        "max_concurrent"
                    ),
                )
            )
    return tuple(instances)


def _ephemeral_agents(
    blueprint: dict[str, Any],
    roles: dict[str, Role],
    crews: dict[str, CrewTemplate],
    scopes: dict[str, Any],
) -> tuple[EphemeralAgentSpec, ...]:
    """One spec per role of every crew the blueprint puts to work (named
    in a hire's `may_spawn` or a workflow's `crew`), scoped by the same
    `tools.<role>` narrowing a standing agent of that role gets."""
    return tuple(
        EphemeralAgentSpec(
            crew=crew_name,
            role=role_name,
            tools=_scope(roles[role_name], scopes),
        )
        for crew_name in sorted(referenced_crews(blueprint))
        for role_name in sorted(crews[crew_name].roles)
    )


def _workflows(blueprint: dict[str, Any]) -> tuple[WorkflowConfig, ...]:
    configs = []
    for name, workflow in sorted((blueprint.get("workflows") or {}).items()):
        configs.append(
            WorkflowConfig(
                name=name,
                enabled=bool(workflow.get("enabled")),
                conductor=workflow.get("conductor", ""),
                crew=workflow.get("crew"),
                # Gate config comes from the blueprint, never hardcoded.
                gates=dict(sorted((workflow.get("gates") or {}).items())),
                max_verify_retries=workflow.get("max_verify_retries"),
            )
        )
    return tuple(configs)


def _merge_delegation(workflows: tuple[WorkflowConfig, ...]) -> dict[str, Any] | None:
    """The one policy-resolved merge gate's delegation, if any workflow
    declares one — what `merge.resolve` compiles from. No delegation
    compiles no rule, so an unattended merge falls to the default deny."""
    for config in workflows:
        gate = config.gates.get("merge") or {}
        if gate.get("resolved_by") == "policy":
            return gate.get("delegate")
    return None


def _heartbeat(blueprint: dict[str, Any]) -> HeartbeatConfig | None:
    heartbeat = blueprint.get("heartbeat")
    if not heartbeat:
        return None
    return HeartbeatConfig(
        conductor=heartbeat["conductor"],
        reconcile_minutes=int(heartbeat["reconcile_minutes"]),
        max_concurrent_runs=int(heartbeat["max_concurrent_runs"]),
        max_attempts=int(heartbeat["max_attempts"]),
        may_start=tuple(sorted(heartbeat["may_start"])),
        daily_cap_usd=float(heartbeat["daily_cap"]["usd"]),
    )


def _budgets(blueprint: dict[str, Any]) -> BudgetRegistration:
    per_run = (blueprint.get("budgets") or {}).get("per_run") or {}
    return BudgetRegistration(
        per_run_usd=float(per_run.get("usd", 0)),
        pause_minutes=int(per_run.get("pause_minutes", 0)),
    )
