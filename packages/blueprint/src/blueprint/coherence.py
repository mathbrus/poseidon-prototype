"""Cross-checks of a blueprint against the versioned catalogs.

Properties of the *document* against a catalog — does every hire name a
role, does every crew list its catalog roles, does every tool scope stay
inside its role's ceiling — as opposed to well-formedness (the JSON
Schema) or company coherence against cluster state (the compiler). Shared
by `blueprint.validate` and `compiler.checks`: the compiler accepts
unvalidated dicts and must fail closed on the same problems, so one
implementation serves both. Malformed values are skipped, never reported:
the schema pass names those.
"""

from typing import Any

from blueprint.catalog import CrewTemplate, Role
from blueprint.tools import CONSOLE_ONLY, KNOWN_TOOLS, PLATFORM_ONLY


def _known(names: dict[str, Any] | tuple[str, ...]) -> str:
    return ", ".join(sorted(names))


def hire_problems(
    document: Any, roles: dict[str, Role], crews: dict[str, CrewTemplate]
) -> list[str]:
    """Every hire names a catalog role, and every crew it may spawn names a
    catalog crew."""
    problems = []
    standing = document.get("standing") or {}
    for name, hire in standing.items() if isinstance(standing, dict) else []:
        if not isinstance(hire, dict) or not isinstance(hire.get("role"), str):
            continue
        if hire["role"] not in roles:
            problems.append(
                f"standing.{name}.role: {hire['role']!r} is not in the role "
                f"catalog (known: {_known(roles)})"
            )
            continue
        for crew in (hire.get("may_spawn") or {}).get("crew") or []:
            if crew not in crews:
                problems.append(
                    f"standing.{name}.may_spawn.crew: {crew!r} is not a known "
                    f"crew (known: {_known(crews)})"
                )
    return problems


def tool_scope_problems(
    document: Any, roles: dict[str, Role], *, report_unknown_tools: bool
) -> list[str]:
    """Every `tools.<role>` names a catalog role and holds a non-empty
    allow-list that narrows the role's catalog tools (never widens them)
    and holds nothing platform-only. Unknown tool names are reported only
    on request: the schema's enum already names them on the validate
    path."""
    problems = []
    scopes = document.get("tools") or {}
    for role_name, scope in scopes.items() if isinstance(scopes, dict) else []:
        if not isinstance(scope, dict):
            continue
        where = f"tools.{role_name}.allow"
        role = roles.get(role_name)
        if role is None:
            problems.append(
                f"tools.{role_name}: {role_name!r} is not in the role catalog "
                f"(known: {_known(roles)})"
            )
            continue
        declared = scope.get("allow")
        tools = (
            [t for t in declared if isinstance(t, str)]
            if isinstance(declared, list)
            else []
        )
        if isinstance(declared, list) and not tools:
            problems.append(f"{where}: must not be empty")
        for tool in tools:
            if tool not in KNOWN_TOOLS:
                if report_unknown_tools:
                    problems.append(f"{where}: {tool!r} is not a tool the gateway serves")
            elif tool in PLATFORM_ONLY | CONSOLE_ONLY:
                continue  # platform_tool_problems names it
            elif tool not in role.tools:
                problems.append(
                    f"{where}: {tool!r} is not in the {role_name!r} role's "
                    f"catalog tools — the blueprint narrows the catalog, never "
                    f"widens it (allowed: {_known(role.tools)})"
                )
        problems += platform_tool_problems(where, tools)
    return problems


def platform_tool_problems(where: str, declared: list[str]) -> list[str]:
    """No company role may hold a platform-only tool: `git.merge_pr` belongs
    to the orchestrator identity after the gate, and a blueprint able to
    grant it would make the merge gate optional. Nor a console-only one:
    `repo.purge` resets the repository for a person deleting the company."""
    return [
        f"{where}: {tool!r} is callable only by the platform identity "
        f"(the orchestrator, after the merge gate) and cannot be granted "
        f"to a company role"
        for tool in declared
        if tool in PLATFORM_ONLY
    ] + [
        f"{where}: {tool!r} is callable only by the console, for a person "
        f"deleting the company, and cannot be granted to a company role"
        for tool in declared
        if tool in CONSOLE_ONLY
    ]


def contract_role_problems(document: Any, roles: dict[str, Role]) -> list[str]:
    """The contract tree's owners name catalog roles — a caller is keyed on
    what it *is*, and an unknown owner would compile a tree nobody can ever
    deliver into: the spec frozen by typo."""
    contract = (document.get("policies") or {}).get("contract_tests")
    declared = contract.get("roles") if isinstance(contract, dict) else None
    return [
        f"policies.contract_tests.roles: {role!r} is not in the role "
        f"catalog (known: {_known(roles)})"
        for role in (declared if isinstance(declared, list) else [])
        if isinstance(role, str) and role not in roles
    ]
