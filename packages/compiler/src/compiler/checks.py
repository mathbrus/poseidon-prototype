"""Compile-time validations.

These are the checks the schema cannot make, because they are about a
company being *coherent* rather than a document being well-formed: does this
workflow have someone to conduct it, does every scope name a tool the
gateway actually serves, can the budget pay for what is enabled.

`compile_blueprint` accepts plain dicts and does not run the schema pass, so
the document-level catalog checks (`schema.coherence`) run here too — the
compiler must fail closed on an unvalidated input, not KeyError halfway
through emission.

Deliberately absent: a child-⊆-parent scope check. A conducting PM is not a
capability supersystem, and ⊆ would force the PM's declared allows to become
the union of every crew member's scope. Spawn authority is `may_spawn` plus the crew
template plus budget and concurrency caps; tool authority is the member's
own compiled allow-list.

The strict lean-roster rule: a standing hire with `count > 0` must be a *spawner*
(its catalog template carries `may_spawn`) — standing executors do not
exist; execution is ephemeral crews, spawned under conductor authority.
Workflow nodes bind to spawners only. `count: 0` placeholders remain legal
documentation.
"""

import re
from typing import Any

from schema.catalog import CREW_TEMPLATES, ROLE_TEMPLATES, CrewTemplate, RoleTemplate
from schema.coherence import (
    contract_role_problems,
    crew_problems,
    platform_tool_problems,
    role_template_problems,
)
from schema.gates import contract_tests_problems, merge_gate_problems
from schema.tools import KNOWN_TOOLS


def check(
    blueprint: dict[str, Any],
    role_catalog: dict[str, RoleTemplate] | None = None,
    crew_catalog: dict[str, CrewTemplate] | None = None,
) -> list[str]:
    """Every problem in the blueprint, as actionable text."""
    roles = role_catalog or ROLE_TEMPLATES
    crews = crew_catalog or CREW_TEMPLATES
    problems: list[str] = []
    problems += _company_name(blueprint)
    # The catalog cross-checks the validate pass also runs — against the
    # *injected* catalog here, so a caller compiling against a different
    # catalog version gets that version's answer. Unknown tool names are
    # reported: the compiler has no schema pass to name them.
    problems += role_template_problems(blueprint, roles, crews)
    problems += crew_problems(blueprint, roles, crews, report_unknown_tools=True)
    problems += contract_role_problems(blueprint, roles)
    problems += _lean_roster(blueprint, roles)
    problems += _tool_scopes(blueprint)
    problems += _spawn_authority(blueprint, roles)
    problems += _spawnable_crews(blueprint, crews)
    problems += _policy_patterns(blueprint)
    problems += _budget_coverage(blueprint)
    problems += _conduct_authority(blueprint, roles)
    # Document-level like the platform-tool rule, repeated here because the
    # compiler accepts unvalidated dicts and must fail closed on a
    # delegation validate_blueprint would have refused.
    problems += merge_gate_problems(blueprint)
    problems += contract_tests_problems(blueprint)
    return problems


def _company_name(blueprint: dict[str, Any]) -> list[str]:
    """`platform` is the bundle key and Rego package the orchestrator's own
    scope lives under — a company by that name would collide
    with it at the bundle server and shadow the platform decision path."""
    name = (blueprint.get("company") or {}).get("name")
    if name == "platform":
        return ["company.name: 'platform' is reserved for the platform policy bundle"]
    return []


def _lean_roster(blueprint: dict[str, Any], roles: dict[str, Any]) -> list[str]:
    """No standing executor counts — every hired (count > 0) role must be
    a spawner, and every enabled workflow's conductor must be one.
    Executors exist only as ephemeral crew members under a conductor's
    authority."""
    problems = []
    for hire, role in _hires(blueprint).items():
        if role.get("count", 0) <= 0:
            continue  # a placeholder documents intent and executes nothing
        template = roles.get(role.get("template"))
        if template is not None and not template.may_spawn:
            problems.append(
                f"departments.roles.{hire}: standing count "
                f"{role.get('count')} on non-spawner template "
                f"{role.get('template')!r} — execution is ephemeral crews; "
                "hire a conductor and give it may_spawn, or set count: 0"
            )
    hired_spawners = _hired_spawners(blueprint, roles)
    for name, wf in _enabled_workflows(blueprint).items():
        conductor = wf.get("conductor")
        if conductor not in hired_spawners:
            available = ", ".join(sorted(hired_spawners)) or "(none hired)"
            problems.append(
                f"workflows.{name}.conductor: {conductor!r} is not a hired "
                f"spawner — enabled workflows bind to a standing conductor "
                f"with count > 0 whose template may spawn (hired: {available})"
            )
    return problems


def _hired_spawners(blueprint: dict[str, Any], roles: dict[str, Any]) -> set[str]:
    return {
        name
        for name, role in _standing_hires(blueprint).items()
        if (t := roles.get(role.get("template"))) is not None and t.may_spawn
    }


def _hires(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return (blueprint.get("departments") or {}).get("roles") or {}


def _standing_hires(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Hired means count > 0. A `count: 0` placeholder documents intent and
    produces no instance, so nothing may bind to it."""
    return {n: r for n, r in _hires(blueprint).items() if r.get("count", 0) > 0}


def _enabled_workflows(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return {
        name: wf
        for name, wf in (blueprint.get("workflows") or {}).items()
        if wf.get("enabled")
    }


def _tool_scopes(blueprint: dict[str, Any]) -> list[str]:
    """Every tool scope names a registered gateway backend, and no company
    role holds a platform-only tool."""
    problems = []
    for role_name, scope in (blueprint.get("tools") or {}).items():
        for tool in (scope or {}).get("allow") or []:
            if tool not in KNOWN_TOOLS:
                problems.append(
                    f"tools.{role_name}.allow: {tool!r} is not a tool the gateway serves"
                )
        problems += platform_tool_problems(
            f"tools.{role_name}.allow", (scope or {}).get("allow") or []
        )
    # A scope for a role nobody hires or crews is dead configuration; say so
    # rather than compiling it into a bundle no caller can ever match. A
    # `count: 0` placeholder counts as unhired — that is what count 0 means.
    usable_roles = {r.get("template") for r in _standing_hires(blueprint).values()}
    usable_roles |= {
        member.get("role_template")
        for crew in (blueprint.get("crews") or {}).values()
        for member in (crew.get("members") or {}).values()
    }
    for role_name in blueprint.get("tools") or {}:
        if role_name not in usable_roles:
            problems.append(
                f"tools.{role_name}: no role template by that name is hired "
                f"or used by a crew, so this scope can never apply"
            )
    return problems


def _spawn_authority(
    blueprint: dict[str, Any], roles: dict[str, RoleTemplate]
) -> list[str]:
    """Spawn authority narrows the template, never widens it — like tools,
    but as an error rather than a silent intersection: a dropped grant that
    a workflow depends on must fail at compile, not at first fulfill."""
    problems = []
    for hire, role in _hires(blueprint).items():
        template = roles.get(role.get("template"))
        if template is None:  # role_template_problems reports it
            continue
        for crew in (role.get("may_spawn") or {}).get("crew") or []:
            if crew not in template.may_spawn:
                allowed = ", ".join(sorted(template.may_spawn)) or "(none)"
                problems.append(
                    f"departments.roles.{hire}.may_spawn.crew: {crew!r} is "
                    f"not in the {template.name!r} template's spawn "
                    f"authority (granted: {allowed})"
                )
    return problems


def _spawnable_crews(
    blueprint: dict[str, Any], crews_catalog: dict[str, CrewTemplate]
) -> list[str]:
    """Every `may_spawn.crew` references a crew this blueprint declares,
    and every enabled workflow's crew is one its conductor may spawn (the
    catalog half — is it a known template — is `schema.coherence`'s)."""
    problems = []
    declared = set(blueprint.get("crews") or {})
    for hire, role in _hires(blueprint).items():
        for crew in (role.get("may_spawn") or {}).get("crew") or []:
            if crew in crews_catalog and crew not in declared:
                problems.append(
                    f"departments.roles.{hire}.may_spawn.crew: {crew!r} is "
                    f"not declared in this blueprint's `crews` section"
                )
    for name, workflow in _enabled_workflows(blueprint).items():
        crew = workflow.get("crew")
        if crew is None:
            continue
        if crew not in declared:
            problems.append(f"workflows.{name}.crew: {crew!r} is not declared in `crews`")
            continue
        conductor = workflow.get("conductor")
        allowed = ((_hires(blueprint).get(conductor) or {}).get("may_spawn") or {}).get(
            "crew"
        ) or []
        if crew not in allowed:
            problems.append(
                f"workflows.{name}: conductor {conductor!r} may not spawn "
                f"{crew!r} — add it to may_spawn.crew, or the workflow "
                f"cannot be fulfilled"
            )
    return problems


def _policy_patterns(blueprint: dict[str, Any]) -> list[str]:
    """`allowed_pattern` must be a valid regex at compile time — a malformed
    one would otherwise surface as an OPA bundle-load failure long after
    apply. Python's `re` is not OPA's RE2, but it rejects the same broken
    syntax an author actually writes."""
    protection = (blueprint.get("policies") or {}).get("branch_protection") or {}
    pattern = protection.get("allowed_pattern")
    if not pattern:
        return []
    try:
        re.compile(pattern)
    except re.error as exc:
        return [
            f"policies.branch_protection.allowed_pattern: not a valid "
            f"regular expression ({exc})"
        ]
    return []


def _conduct_authority(
    blueprint: dict[str, Any], roles: dict[str, RoleTemplate]
) -> list[str]:
    """The heartbeat's declared authority must be coherent: a hired spawner
    to decide, enabled workflows to start, and a daily cap that can fund at
    least one run — a cap below the per-run budget makes the section dead
    configuration, and dead configuration is an error, not documentation."""
    conduct = blueprint.get("conduct")
    if not conduct:
        return []
    problems = []
    conductor = conduct.get("conductor")
    if conductor not in _hired_spawners(blueprint, roles):
        problems.append(
            f"conduct.conductor: {conductor!r} is not a hired spawner — "
            "the heartbeat dispatches a standing conductor for its decisions"
        )
    enabled = _enabled_workflows(blueprint)
    for workflow in conduct.get("may_start") or []:
        if workflow not in enabled:
            problems.append(
                f"conduct.may_start: {workflow!r} is not an enabled workflow "
                "in this blueprint, so the grant could never start anything"
            )
    per_run = ((blueprint.get("budgets") or {}).get("per_run") or {}).get("usd") or 0
    daily = (conduct.get("daily_cap") or {}).get("usd") or 0
    if daily and per_run and daily < per_run:
        problems.append(
            f"conduct.daily_cap.usd: {daily:g} cannot fund a single run "
            f"(budgets.per_run.usd is {per_run:g}) — the heartbeat could "
            "never start one"
        )
    return problems


def _budget_coverage(blueprint: dict[str, Any]) -> list[str]:
    """Budgets cover enabled workflows: a run cap must exist and be
    positive, or the first hard-stop check has nothing to compare against."""
    problems = []
    budgets = blueprint.get("budgets") or {}
    per_run = (budgets.get("per_run") or {}).get("usd")
    if _enabled_workflows(blueprint) and not per_run:
        problems.append(
            "budgets.per_run.usd: enabled workflows need a per-run cap "
            "(the LLM gateway hard-stops against it)"
        )
    return problems
