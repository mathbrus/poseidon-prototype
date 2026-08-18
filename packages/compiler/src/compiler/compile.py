"""The compiler: `(blueprint, role_catalog, registry_snapshot) → plan`.

A pure function. It reads nothing but its arguments, writes nothing,
and touches no cluster: apply is the operator's job, and keeping the
mutation in exactly one place is what makes "the blueprint is the source
of truth" checkable rather than aspirational.

Secrets never enter a plan. Agent specs carry a role, a tier and a tool
scope; credentials arrive at runtime as projected tokens.
"""

from typing import Any

from compiler.checks import check
from compiler.errors import CompileError
from compiler.infra import build_infra
from compiler.plan import (
    AgentInstanceCR,
    BudgetRegistration,
    CompanyCR,
    CrewMemberSpec,
    DeploymentPlan,
    RegoBundle,
    WorkflowConfig,
)
from compiler.rego import build_bundle, build_platform_bundle
from compiler.snapshot import EMPTY, RegistrySnapshot
from schema.catalog import CREW_TEMPLATES, ROLE_TEMPLATES, CrewTemplate, RoleTemplate
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
        "/admin/sandboxes",
        "/admin/sandboxes.delete",
    }
)

DEFAULT_VERIFY_RETRIES = 2
DEFAULT_DRAIN = {"on_conductor_drain": "abort_crew", "timeout_minutes": 30}


def compile_blueprint(
    blueprint: dict[str, Any],
    role_catalog: dict[str, RoleTemplate] | None = None,
    crew_catalog: dict[str, CrewTemplate] | None = None,
    snapshot: RegistrySnapshot = EMPTY,
) -> DeploymentPlan:
    roles = role_catalog or ROLE_TEMPLATES
    crews_catalog = crew_catalog or CREW_TEMPLATES

    problems = check(blueprint, roles, crews_catalog)
    if problems:
        raise CompileError(problems)

    company_section = blueprint["company"]
    company = company_section["name"]
    namespace = company
    company_cr = CompanyCR(
        name=company,
        namespace=namespace,
        mission=company_section["mission"],
        stage=company_section.get("stage"),
    )

    scopes = blueprint.get("tools") or {}
    instances = _instances(blueprint, roles, scopes, company, snapshot)
    crew_members = _crew_members(blueprint, crews_catalog)
    workflows = _workflows(blueprint)
    budgets = _budgets(blueprint)
    infra = build_infra(blueprint, namespace)
    bundle = RegoBundle(
        files=build_bundle(
            company=company,
            instances=list(instances),
            crew_members=list(crew_members),
            policies=blueprint.get("policies") or {},
        )
    )
    return DeploymentPlan(
        company=company_cr,
        instances=instances,
        crew_members=crew_members,
        workflows=workflows,
        budgets=budgets,
        infra=infra,
        bundle=bundle,
        platform_bundle=RegoBundle(files=build_platform_bundle(PLATFORM_SCOPE)),
    )


def _instances(
    blueprint: dict[str, Any],
    roles: dict[str, RoleTemplate],
    scopes: dict[str, Any],
    company: str,
    snapshot: RegistrySnapshot,
) -> tuple[AgentInstanceCR, ...]:
    """Standing roster only. A `count: 0` placeholder produces no instance —
    that is the whole point of declaring one."""
    instances = []
    for hire, role in sorted(
        (blueprint.get("departments") or {}).get("roles", {}).items()
    ):
        count = role.get("count", 0)
        template = roles[role["template"]]
        # The blueprint narrows the template, never widens it: a scope the
        # catalog does not grant cannot be granted by naming it here. The
        # same holds for spawn authority — checks.py errors on a widening
        # attempt; the intersection is the belt under that brace.
        declared = set(
            (scopes.get(role["template"]) or {}).get("allow") or template.tools
        )
        tools = tuple(sorted(declared & set(template.tools)))
        may_spawn = tuple(
            sorted(
                set((role.get("may_spawn") or {}).get("crew") or [])
                & set(template.may_spawn)
            )
        )
        # Existing ids are reused in order so an apply is stable; minted ids
        # skip anything already taken — a count change must never produce
        # two instances sharing an agent_id (the diff keys on it).
        agent_ids = [i.agent_id for i in snapshot.instances_for(company, hire)][:count]
        serial = 0
        while len(agent_ids) < count:
            serial += 1
            candidate = f"{hire}-{serial}"
            if candidate not in agent_ids:
                agent_ids.append(candidate)
        for agent_id in agent_ids:
            instances.append(
                AgentInstanceCR(
                    agent_id=agent_id,
                    company=company,
                    hire=hire,
                    role_template=role["template"],
                    model_tier=role.get("model_tier") or template.model_tier,
                    tools=tools,
                    may_spawn=may_spawn,
                    max_concurrent_spawns=(role.get("may_spawn") or {}).get(
                        "max_concurrent"
                    ),
                    runtime_hints=dict(template.runtime_hints),
                )
            )
    return tuple(instances)


def _crew_members(
    blueprint: dict[str, Any], crews_catalog: dict[str, CrewTemplate]
) -> tuple[CrewMemberSpec, ...]:
    members = []
    for crew_name, crew in sorted((blueprint.get("crews") or {}).items()):
        catalog_crew = crews_catalog.get(crew_name)
        for member_name, member in sorted((crew.get("members") or {}).items()):
            catalog_member = (
                catalog_crew.members.get(member_name) if catalog_crew else None
            )
            needs_sandbox = member.get("needs_sandbox")
            if needs_sandbox is None:
                needs_sandbox = catalog_member.needs_sandbox if catalog_member else True
            # Narrowed against the catalog member like standing tools are
            # narrowed against the role template: a widening blueprint is a
            # checks.py error, and even past that, nothing wider than the
            # catalog's allow-list may reach a compiled bundle.
            declared = set(member.get("tools") or [])
            if catalog_member is not None:
                declared &= set(catalog_member.tools)
            members.append(
                CrewMemberSpec(
                    crew=crew_name,
                    member=member_name,
                    role_template=member["role_template"],
                    tools=tuple(sorted(declared)),
                    needs_sandbox=bool(needs_sandbox),
                    budget_inheritance=crew.get("budget_inheritance", "run"),
                )
            )
    return tuple(members)


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
                max_verify_retries=int(
                    workflow.get("max_verify_retries", DEFAULT_VERIFY_RETRIES)
                ),
                drain=dict(
                    sorted({**DEFAULT_DRAIN, **(workflow.get("drain") or {})}.items())
                ),
            )
        )
    return tuple(configs)


def _budgets(blueprint: dict[str, Any]) -> BudgetRegistration:
    budgets = blueprint.get("budgets") or {}
    return BudgetRegistration(
        per_run_usd=float((budgets.get("per_run") or {}).get("usd", 0)),
    )
