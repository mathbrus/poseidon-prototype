"""Compile-time validations.

These are the checks the schema cannot make, because they are about a
company being *coherent* rather than a document being well-formed: is the
conductor an active standing spawner, does every scope name a tool the
gateway actually serves, can the budget pay for a run.

`compile_blueprint` accepts plain dicts and does not run the schema pass, so
the document-level catalog checks (`schema.coherence`) run here too — the
compiler must fail closed on an unvalidated input, not KeyError halfway
through emission.

Deliberately absent: a child-⊆-parent scope check. A conductor is not a
capability supersystem, and ⊆ would force the PM's declared allows to become
the union of every ephemeral agent's scope. Spawn authority is `may_spawn` plus the crew
template plus budget and concurrency caps; tool authority is the agent's
own compiled allow-list.

The strict lean-standing rule: a standing agent with `count > 0` must be a
*spawner* (its catalog role carries `may_spawn`) — standing coders do not
exist; execution is ephemeral crews, spawned under conductor authority.
The conductor (`heartbeat.conductor`) is a spawner. `count: 0`
placeholders remain legal documentation.
"""

import re
from typing import Any

from schema.catalog import CREW_TEMPLATES, ROLES, STEWARDSHIP_CREW, CrewTemplate, Role
from schema.coherence import (
    contract_role_problems,
    hire_problems,
    tool_scope_problems,
)
from schema.gates import contract_tests_problems, document_problems, merge_gate_problems


def check(
    blueprint: dict[str, Any],
    role_catalog: dict[str, Role] | None = None,
    crew_catalog: dict[str, CrewTemplate] | None = None,
) -> list[str]:
    """Every problem in the blueprint, as actionable text."""
    roles = role_catalog or ROLES
    crews = crew_catalog or CREW_TEMPLATES
    problems: list[str] = []
    # The catalog cross-checks the validate pass also runs — against the
    # *injected* catalog here, so a caller compiling against a different
    # catalog version gets that version's answer. Unknown tool names are
    # reported: the compiler has no schema pass to name them.
    problems += hire_problems(blueprint, roles, crews)
    problems += tool_scope_problems(blueprint, roles, report_unknown_tools=True)
    problems += contract_role_problems(blueprint, roles)
    problems += _lean_standing(blueprint, roles)
    problems += _dead_scopes(blueprint)
    problems += _spawn_authority(blueprint, roles)
    problems += _spawnable_crews(blueprint, crews)
    problems += _policy_patterns(blueprint)
    problems += _budget_coverage(blueprint)
    problems += _gate_deadlines(blueprint)
    problems += _toolchain(blueprint)
    problems += _heartbeat_authority(blueprint, roles)
    # Document-level like the platform-tool rule, repeated here because the
    # compiler accepts unvalidated dicts and must fail closed on a
    # delegation validate_blueprint would have refused.
    problems += merge_gate_problems(blueprint)
    problems += contract_tests_problems(blueprint)
    problems += document_problems(blueprint)
    return problems


def _lean_standing(blueprint: dict[str, Any], roles: dict[str, Any]) -> list[str]:
    """No standing coder counts — every active (count > 0) hire must be a
    spawner. Coding roles exist only as ephemeral agents under the
    conductor's authority."""
    problems = []
    for standing_name, hire in _hires(blueprint).items():
        if hire.get("count", 0) <= 0:
            continue  # a placeholder documents intent and executes nothing
        role = roles.get(hire.get("role"))
        if role is not None and not role.may_spawn:
            problems.append(
                f"standing.{standing_name}: standing count "
                f"{hire.get('count')} on non-spawner role "
                f"{hire.get('role')!r} — execution is ephemeral crews; "
                "declare a conductor with may_spawn, or set count: 0"
            )
    return problems


def _active_spawners(blueprint: dict[str, Any], roles: dict[str, Any]) -> set[str]:
    return {
        name
        for name, hire in _active_hires(blueprint).items()
        if (r := roles.get(hire.get("role"))) is not None and r.may_spawn
    }


def _hires(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    return blueprint.get("standing") or {}


def _active_hires(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    """Hired means count > 0. A `count: 0` placeholder documents intent and
    produces no instance, so nothing may bind to it."""
    return {n: r for n, r in _hires(blueprint).items() if r.get("count", 0) > 0}


def _workflows(blueprint: dict[str, Any]) -> dict[str, dict[str, Any]]:
    workflows = blueprint.get("workflows") or {}
    return {n: wf for n, wf in workflows.items() if isinstance(wf, dict)}


def _conductor(blueprint: dict[str, Any]) -> str | None:
    heartbeat = blueprint.get("heartbeat")
    return heartbeat.get("conductor") if isinstance(heartbeat, dict) else None


def referenced_crews(blueprint: dict[str, Any]) -> set[str]:
    """The crews a blueprint puts to work: every crew a hire may spawn and
    every crew a workflow executes through. The catalog defines what a crew
    is; the blueprint only names which ones exist for this company."""
    named = {
        crew
        for hire in (blueprint.get("standing") or {}).values()
        for crew in (hire.get("may_spawn") or {}).get("crew") or []
    }
    named |= {
        workflow["crew"]
        for workflow in (blueprint.get("workflows") or {}).values()
        if workflow.get("crew")
    }
    return named


def _dead_scopes(blueprint: dict[str, Any]) -> list[str]:
    """A scope no active hire and no crew uses is dead configuration; say
    so rather than compiling it into a bundle no caller can ever match. A
    `count: 0` placeholder is inactive — that is what count 0 means."""
    usable_roles = {h.get("role") for h in _active_hires(blueprint).values()}
    usable_roles |= {
        role
        for crew in referenced_crews(blueprint)
        if crew in CREW_TEMPLATES
        for role in CREW_TEMPLATES[crew].roles
    }
    return [
        f"tools.{role_name}: no active hire or crew uses that role, "
        f"so this scope can never apply"
        for role_name in blueprint.get("tools") or {}
        if role_name not in usable_roles
    ]


def _spawn_authority(blueprint: dict[str, Any], roles: dict[str, Role]) -> list[str]:
    """Spawn authority narrows the role, never widens it — like tools, but
    as an error rather than a silent intersection: a dropped grant that a
    workflow depends on must fail at compile, not at first fulfill."""
    problems = []
    for standing_name, hire in _hires(blueprint).items():
        role = roles.get(hire.get("role"))
        if role is None:  # hire_problems reports it
            continue
        for crew in (hire.get("may_spawn") or {}).get("crew") or []:
            if crew not in role.may_spawn:
                allowed = ", ".join(sorted(role.may_spawn)) or "(none)"
                problems.append(
                    f"standing.{standing_name}.may_spawn.crew: {crew!r} is "
                    f"not in the {role.name!r} role's spawn "
                    f"authority (granted: {allowed})"
                )
    return problems


def _spawnable_crews(
    blueprint: dict[str, Any], crews_catalog: dict[str, CrewTemplate]
) -> list[str]:
    """Every crew the company needs is one the conductor may spawn: each
    workflow's crew, and the stewardship crew the platform's document
    tickets route to — otherwise the work compiles but can never be
    fulfilled. (Whether a `may_spawn.crew` names a known crew is
    `schema.coherence`'s.)"""
    problems = []
    conductor = _conductor(blueprint)
    allowed = ((_hires(blueprint).get(conductor) or {}).get("may_spawn") or {}).get(
        "crew"
    ) or []
    # A missing or unknown conductor is `_heartbeat_authority`'s problem;
    # the document crew is asked of a conductor that exists.
    if conductor in _hires(blueprint) and STEWARDSHIP_CREW not in allowed:
        problems.append(
            f"heartbeat.conductor: {conductor!r} may not spawn {STEWARDSHIP_CREW!r} — "
            f"the platform's bootstrap and architecture-request tickets route to "
            f"that crew; add it to may_spawn.crew, or the company can never write "
            f"its documents"
        )
    for name, workflow in _workflows(blueprint).items():
        crew = workflow.get("crew")
        if crew is None:
            continue
        if crew not in crews_catalog:
            problems.append(
                f"workflows.{name}.crew: {crew!r} is not a known crew "
                f"(known: {', '.join(sorted(crews_catalog))})"
            )
            continue
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


def _heartbeat_authority(blueprint: dict[str, Any], roles: dict[str, Role]) -> list[str]:
    """Every company has a heartbeat, and its declared authority must be
    coherent: an active standing spawner to conduct, and a daily cap that
    can fund at least one run — a cap below the per-run budget makes the
    section dead configuration, and dead configuration is an error, not
    documentation."""
    heartbeat = blueprint.get("heartbeat")
    if not isinstance(heartbeat, dict):
        return ["heartbeat: required — a company always has a heartbeat"]
    problems = []
    conductor = heartbeat.get("conductor")
    if conductor not in _active_spawners(blueprint, roles):
        available = ", ".join(sorted(_active_spawners(blueprint, roles))) or "(none)"
        problems.append(
            f"heartbeat.conductor: {conductor!r} is not an active standing spawner — "
            "a hire with count > 0 whose role may spawn, dispatched to decide "
            f"and to scope and the authority every crew is fulfilled under "
            f"(active: {available})"
        )
    per_run = ((blueprint.get("budgets") or {}).get("per_run") or {}).get("usd") or 0
    daily = (heartbeat.get("daily_cap") or {}).get("usd") or 0
    if daily and per_run and daily < per_run:
        problems.append(
            f"heartbeat.daily_cap.usd: {daily:g} cannot fund a single run "
            f"(budgets.per_run.usd is {per_run:g}) — the heartbeat could "
            "never start one"
        )
    return problems


def _budget_coverage(blueprint: dict[str, Any]) -> list[str]:
    """A run cap must exist and be positive, or the first hard-stop check
    has nothing to compare against; and the pause a stopped run waits out
    must be declared, or the workflow would park with no deadline."""
    problems = []
    per_run = (blueprint.get("budgets") or {}).get("per_run") or {}
    if not per_run.get("usd"):
        problems.append(
            "budgets.per_run.usd: every run needs a per-run cap "
            "(the LLM gateway hard-stops against it)"
        )
    if not per_run.get("pause_minutes"):
        problems.append(
            "budgets.per_run.pause_minutes: a run stopped at the cap needs a "
            "declared wait for the human who may raise it"
        )
    return problems


def _toolchain(blueprint: dict[str, Any]) -> list[str]:
    """The sandbox image and the test command are declared, never
    defaulted: what a run executes in, and what the verify gate runs, are
    the company's to say."""
    toolchain = (blueprint.get("stack") or {}).get("toolchain") or {}
    return [
        f"stack.toolchain.{field}: required — the sandbox runs what the company declares"
        for field in ("image", "test_command")
        if not toolchain.get(field)
    ]


def _gate_deadlines(blueprint: dict[str, Any]) -> list[str]:
    """Every declared gate names how long it waits for a human — the
    workflow parks on that timer and escalates at half of it, so an
    undeclared deadline would be a park with no end."""
    problems = []
    for name, node in (blueprint.get("workflows") or {}).items():
        for gate, config in (node.get("gates") or {}).items():
            if not (config or {}).get("deadline_minutes"):
                problems.append(
                    f"workflows.{name}.gates.{gate}.deadline_minutes: a declared "
                    "gate needs the wait it parks for"
                )
    return problems
