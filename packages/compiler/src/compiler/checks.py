"""Compile-time validations (§4.1).

These are the checks the schema cannot make, because they are about a
company being *coherent* rather than a document being well-formed: does this
workflow have someone to conduct it, does every scope name a tool the
gateway actually serves, can the budget pay for what is enabled.

Deliberately absent: the child-⊆-parent scope check. It was dropped with the
lean roster (§4.5 rule 5) — a conducting PM is not a capability supersystem,
and keeping ⊆ would force the PM's declared allows to become the union of
every crew member's scope. Spawn authority is `may_spawn` plus the crew
template plus budget and concurrency caps; tool authority is the member's
own compiled allow-list.

Also deliberately absent until plan step 19: the strict lean-roster rule
that workflow nodes bind to standing *spawners* and no standing executor may
exist. The M2→M3 bridge blueprint hires a transitional engineer, and it must
compile until the firing diff removes it (sequencing choice 7).
"""

from typing import Any

from schema.catalog import CREW_TEMPLATES, ROLE_TEMPLATES
from schema.tools import KNOWN_TOOLS, PLATFORM_ONLY


def check(blueprint: dict[str, Any]) -> list[str]:
    """Every problem in the blueprint, as actionable text."""
    problems: list[str] = []
    problems += _workflow_conductors(blueprint)
    problems += _tool_scopes(blueprint)
    problems += _spawnable_crews(blueprint)
    problems += _crew_allow_lists(blueprint)
    problems += _budget_coverage(blueprint)
    return problems


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


def _workflow_conductors(blueprint: dict[str, Any]) -> list[str]:
    """Every workflow node maps to a hired standing role — never to an
    ephemeral member role (§4.5 rule 4)."""
    problems = []
    hired = _standing_hires(blueprint)
    crew_member_templates = {
        member.role_template
        for crew in CREW_TEMPLATES.values()
        for member in crew.members.values()
    }
    for name, workflow in _enabled_workflows(blueprint).items():
        conductor = workflow.get("conductor")
        if conductor not in hired:
            available = ", ".join(sorted(hired)) or "(none hired)"
            problems.append(
                f"workflows.{name}.conductor: {conductor!r} is not a hired "
                f"standing role — enabled workflows need a conductor with "
                f"count > 0 (hired: {available})"
            )
            continue
        template = hired[conductor].get("template")
        if template in crew_member_templates and template not in {"pm"}:
            # Not fatal at M2 (the bridge hires an engineer), but binding a
            # workflow to an executor template is the shape step 19 forbids.
            problems.append(
                f"workflows.{name}.conductor: {conductor!r} uses the "
                f"{template!r} template, which is crew-member material; "
                f"workflow nodes bind to standing spawners"
            )
    return problems


def _tool_scopes(blueprint: dict[str, Any]) -> list[str]:
    """Every tool scope names a registered gateway backend, and no company
    role holds a platform-only tool."""
    problems = []
    for role_name, scope in (blueprint.get("tools") or {}).items():
        for tool in (scope or {}).get("allow") or []:
            if tool not in KNOWN_TOOLS:
                problems.append(
                    f"tools.{role_name}.allow: {tool!r} is not a tool the "
                    f"gateway serves"
                )
            elif tool in PLATFORM_ONLY:
                problems.append(
                    f"tools.{role_name}.allow: {tool!r} is reserved for the "
                    f"platform identity"
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


def _spawnable_crews(blueprint: dict[str, Any]) -> list[str]:
    """Every `may_spawn.crew` references a crew this blueprint declares and
    the catalog knows."""
    problems = []
    declared = set(blueprint.get("crews") or {})
    for hire, role in _hires(blueprint).items():
        for crew in (role.get("may_spawn") or {}).get("crew") or []:
            if crew not in CREW_TEMPLATES:
                problems.append(
                    f"departments.roles.{hire}.may_spawn.crew: {crew!r} is "
                    f"not a known crew template"
                )
            elif crew not in declared:
                problems.append(
                    f"departments.roles.{hire}.may_spawn.crew: {crew!r} is "
                    f"not declared in this blueprint's `crews` section"
                )
    for name, workflow in _enabled_workflows(blueprint).items():
        crew = workflow.get("crew")
        if crew is None:
            continue
        if crew not in declared:
            problems.append(
                f"workflows.{name}.crew: {crew!r} is not declared in `crews`"
            )
            continue
        conductor = workflow.get("conductor")
        allowed = (
            (_hires(blueprint).get(conductor) or {}).get("may_spawn") or {}
        ).get("crew") or []
        if crew not in allowed:
            problems.append(
                f"workflows.{name}: conductor {conductor!r} may not spawn "
                f"{crew!r} — add it to may_spawn.crew, or the workflow "
                f"cannot be fulfilled"
            )
    return problems


def _crew_allow_lists(blueprint: dict[str, Any]) -> list[str]:
    """Every crew member carries an explicit allow-list of real tools. No
    ⊆-parent check by design (§4.5 rule 5)."""
    problems = []
    for crew_name, crew in (blueprint.get("crews") or {}).items():
        for member_name, member in (crew.get("members") or {}).items():
            where = f"crews.{crew_name}.members.{member_name}"
            tools = member.get("tools") or []
            if not tools:
                problems.append(f"{where}.tools: must not be empty")
            for tool in tools:
                if tool not in KNOWN_TOOLS:
                    problems.append(
                        f"{where}.tools: {tool!r} is not a tool the gateway serves"
                    )
                elif tool in PLATFORM_ONLY:
                    problems.append(
                        f"{where}.tools: {tool!r} is reserved for the platform identity"
                    )
            if member.get("role_template") not in ROLE_TEMPLATES:
                problems.append(
                    f"{where}.role_template: "
                    f"{member.get('role_template')!r} is not in the role catalog"
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
    monthly = budgets.get("per_role_monthly") or {}
    hired = _standing_hires(blueprint)
    for role_name in monthly:
        if role_name not in hired:
            problems.append(
                f"budgets.per_role_monthly.{role_name}: no standing role by "
                f"that name is hired"
            )
    return problems
