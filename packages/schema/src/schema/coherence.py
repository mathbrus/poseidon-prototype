"""Cross-checks of a blueprint against the versioned catalogs.

Properties of the *document* against a catalog — does every hire name a
template, does every ephemeral agent stay inside its catalog agent's
allow-list — as opposed to well-formedness (the JSON Schema) or company
coherence against cluster state (the compiler). Shared by `schema.validate`
and `compiler.checks`: the compiler accepts unvalidated dicts and must fail
closed on the same problems, so one implementation serves both. Malformed
values are skipped, never reported: the schema pass names those.
"""

from typing import Any

from schema.catalog import CrewTemplate, RoleTemplate
from schema.tools import KNOWN_TOOLS, PLATFORM_ONLY


def _known(names: dict[str, Any]) -> str:
    return ", ".join(sorted(names))


def role_template_problems(
    document: Any, roles: dict[str, RoleTemplate], crews: dict[str, CrewTemplate]
) -> list[str]:
    """Every hire names a catalog role template, and every crew it may spawn
    names a catalog crew template."""
    problems = []
    hires = (document.get("departments") or {}).get("roles") or {}
    for hire, role in hires.items() if isinstance(hires, dict) else []:
        if not isinstance(role, dict) or not isinstance(role.get("template"), str):
            continue
        if role["template"] not in roles:
            problems.append(
                f"departments.roles.{hire}.template: {role['template']!r} is "
                f"not in the role catalog (known: {_known(roles)})"
            )
            continue
        for crew in (role.get("may_spawn") or {}).get("crew") or []:
            if crew not in crews:
                problems.append(
                    f"departments.roles.{hire}.may_spawn.crew: {crew!r} is "
                    f"not a known crew template (known: {_known(crews)})"
                )
    return problems


def crew_problems(
    document: Any,
    roles: dict[str, RoleTemplate],
    crews: dict[str, CrewTemplate],
    *,
    report_unknown_tools: bool,
) -> list[str]:
    """Every `crews:` section names a catalog crew; every agent is one of
    that crew's agents, on the catalog's role template, with a non-empty
    allow-list that narrows the catalog agent's (never widens it) and holds
    nothing platform-only. Unknown tool names are reported only on request:
    the schema's enum already names them on the validate path."""
    problems = []
    declared = document.get("crews") or {}
    for crew_name, crew in declared.items() if isinstance(declared, dict) else []:
        if not isinstance(crew, dict):
            continue
        catalog_crew = crews.get(crew_name)
        if catalog_crew is None:
            problems.append(
                f"crews.{crew_name}: not a known crew template (known: {_known(crews)})"
            )
        agents = crew.get("agents")
        for agent_name, agent in (agents if isinstance(agents, dict) else {}).items():
            if not isinstance(agent, dict):
                continue
            where = f"crews.{crew_name}.agents.{agent_name}"
            catalog_agent = catalog_crew.agents.get(agent_name) if catalog_crew else None
            if catalog_crew is not None and catalog_agent is None:
                problems.append(
                    f"{where}: not a agent of the {crew_name!r} crew "
                    f"template (agents: {_known(catalog_crew.agents)})"
                )
            role_template = agent.get("role_template")
            if isinstance(role_template, str) and role_template not in roles:
                problems.append(
                    f"{where}.role_template: {role_template!r} is not in the "
                    f"role catalog (known: {_known(roles)})"
                )
            elif (
                catalog_agent is not None
                and isinstance(role_template, str)
                and role_template != catalog_agent.role_template
            ):
                problems.append(
                    f"{where}.role_template: {role_template!r} does not match "
                    f"the catalog's {catalog_agent.role_template!r} for this "
                    f"agent — the blueprint names the template, the catalog "
                    f"defines it"
                )
            declared_tools = agent.get("tools")
            tools = (
                [t for t in declared_tools if isinstance(t, str)]
                if isinstance(declared_tools, list)
                else []
            )
            if isinstance(declared_tools, list) and not tools:
                problems.append(f"{where}.tools: must not be empty")
            for tool in tools:
                if tool not in KNOWN_TOOLS:
                    if report_unknown_tools:
                        problems.append(
                            f"{where}.tools: {tool!r} is not a tool the gateway serves"
                        )
                elif tool in PLATFORM_ONLY:
                    continue  # platform_tool_problems names it
                elif catalog_agent is not None and tool not in catalog_agent.tools:
                    problems.append(
                        f"{where}.tools: {tool!r} is not in the catalog agent's "
                        f"allow-list — the blueprint narrows the catalog, never "
                        f"widens it (allowed: {_known(catalog_agent.tools)})"
                    )
            problems += platform_tool_problems(f"{where}.tools", tools)
    return problems


def platform_tool_problems(where: str, declared: list[str]) -> list[str]:
    """No company role may hold a platform-only tool: `git.merge_pr` belongs
    to the orchestrator identity after the gate, and a blueprint able to
    grant it would make the merge gate optional."""
    return [
        f"{where}: {tool!r} is callable only by the platform identity "
        f"(the orchestrator, after the merge gate) and cannot be granted "
        f"to a company role"
        for tool in declared
        if tool in PLATFORM_ONLY
    ]


def contract_role_problems(document: Any, roles: dict[str, RoleTemplate]) -> list[str]:
    """The contract tree's owners name catalog role templates — a caller is
    keyed on what it *is*, and an unknown owner would compile a tree nobody
    can ever deliver into: the spec frozen by typo."""
    contract = (document.get("policies") or {}).get("contract_tests")
    declared = contract.get("roles") if isinstance(contract, dict) else None
    return [
        f"policies.contract_tests.roles: {role!r} is not in the role "
        f"catalog (known: {_known(roles)})"
        for role in (declared if isinstance(declared, list) else [])
        if isinstance(role, str) and role not in roles
    ]
