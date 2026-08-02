"""Compile-time validations (§4.1).

These are the checks the schema cannot make, because they are about a
company being *coherent* rather than a document being well-formed: does this
workflow have someone to conduct it, does every scope name a tool the
gateway actually serves, can the budget pay for what is enabled.

`compile_blueprint` accepts plain dicts and does not run the schema pass, so
a few document-level checks (template existence, tool vocabulary) are
repeated here — the compiler must fail closed on an unvalidated input, not
KeyError halfway through emission.

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

import re
from typing import Any

from schema.catalog import CREW_TEMPLATES, ROLE_TEMPLATES, CrewTemplate, RoleTemplate
from schema.tools import KNOWN_TOOLS, PLATFORM_ONLY


def check(
    blueprint: dict[str, Any],
    role_catalog: dict[str, RoleTemplate] | None = None,
    crew_catalog: dict[str, CrewTemplate] | None = None,
) -> list[str]:
    """Every problem in the blueprint, as actionable text."""
    roles = role_catalog or ROLE_TEMPLATES
    crews = crew_catalog or CREW_TEMPLATES
    problems: list[str] = []
    problems += _role_templates(blueprint, roles)
    problems += _workflow_conductors(blueprint)
    problems += _tool_scopes(blueprint)
    problems += _spawn_authority(blueprint, roles)
    problems += _spawnable_crews(blueprint, crews)
    problems += _crew_allow_lists(blueprint, roles, crews)
    problems += _policy_patterns(blueprint)
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


def _role_templates(
    blueprint: dict[str, Any], roles: dict[str, RoleTemplate]
) -> list[str]:
    """Every hire names a catalog template — checked against the *injected*
    catalog, so a caller compiling against a different catalog version gets
    that version's answer, not the module default's."""
    return [
        f"departments.roles.{hire}.template: {role.get('template')!r} is "
        f"not in the role catalog (known: {', '.join(sorted(roles))})"
        for hire, role in _hires(blueprint).items()
        if role.get("template") not in roles
    ]


def _workflow_conductors(blueprint: dict[str, Any]) -> list[str]:
    """Every workflow node maps to a hired standing role — never to an
    ephemeral member role (§4.5 rule 4). The stricter spawner-only stance
    (no standing executor may conduct) is plan step 19's compile error,
    together with the firing diff — not here."""
    problems = []
    hired = _standing_hires(blueprint)
    for name, workflow in _enabled_workflows(blueprint).items():
        conductor = workflow.get("conductor")
        if conductor not in hired:
            available = ", ".join(sorted(hired)) or "(none hired)"
            problems.append(
                f"workflows.{name}.conductor: {conductor!r} is not a hired "
                f"standing role — enabled workflows need a conductor with "
                f"count > 0 (hired: {available})"
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


def _spawn_authority(
    blueprint: dict[str, Any], roles: dict[str, RoleTemplate]
) -> list[str]:
    """Spawn authority narrows the template, never widens it — like tools,
    but as an error rather than a silent intersection: a dropped grant that
    a workflow depends on must fail at compile, not at first fulfill."""
    problems = []
    for hire, role in _hires(blueprint).items():
        template = roles.get(role.get("template"))
        if template is None:  # _role_templates reports it
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
    """Every `may_spawn.crew` references a crew this blueprint declares and
    the catalog knows — and so does every `crews:` section key, or the
    compiler would resolve it against nothing."""
    problems = []
    declared = set(blueprint.get("crews") or {})
    known = ", ".join(sorted(crews_catalog))
    for crew_name in sorted(declared):
        if crew_name not in crews_catalog:
            problems.append(
                f"crews.{crew_name}: not a known crew template (known: {known})"
            )
    for hire, role in _hires(blueprint).items():
        for crew in (role.get("may_spawn") or {}).get("crew") or []:
            if crew not in crews_catalog:
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


def _crew_allow_lists(
    blueprint: dict[str, Any],
    roles: dict[str, RoleTemplate],
    crews_catalog: dict[str, CrewTemplate],
) -> list[str]:
    """Every crew member carries an explicit allow-list of real tools that
    narrows (never widens) the catalog member's contract. No ⊆-parent check
    by design (§4.5 rule 5)."""
    problems = []
    for crew_name, crew in (blueprint.get("crews") or {}).items():
        catalog_crew = crews_catalog.get(crew_name)
        for member_name, member in (crew.get("members") or {}).items():
            where = f"crews.{crew_name}.members.{member_name}"
            catalog_member = (
                catalog_crew.members.get(member_name) if catalog_crew else None
            )
            if catalog_crew is not None and catalog_member is None:
                problems.append(
                    f"{where}: not a member of the {crew_name!r} crew "
                    f"template (members: "
                    f"{', '.join(sorted(catalog_crew.members))})"
                )
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
                elif catalog_member is not None and tool not in catalog_member.tools:
                    problems.append(
                        f"{where}.tools: {tool!r} is not in the catalog "
                        f"member's allow-list — the blueprint narrows the "
                        f"catalog, never widens it"
                    )
            if member.get("role_template") not in roles:
                problems.append(
                    f"{where}.role_template: "
                    f"{member.get('role_template')!r} is not in the role catalog"
                )
            elif (
                catalog_member is not None
                and member.get("role_template") != catalog_member.role_template
            ):
                problems.append(
                    f"{where}.role_template: {member.get('role_template')!r} "
                    f"does not match the catalog's "
                    f"{catalog_member.role_template!r} for this member"
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
