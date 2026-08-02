"""Blueprint validation with errors a human can act on.

`jsonschema`'s default messages name a failing keyword deep in a combined
schema; what an author needs is *where* in their document and *what to
write instead*. So every error is rendered as a YAML-ish path plus the
concrete allowed values, and errors are reported together — fixing a
blueprint one message per run is a bad trade for the author.

Structural checks only (plan step 7). The catalog cross-checks (does this
template exist, is `analyst` really at count 0) live here too, because they
are properties of the *document* against a versioned catalog; anything
needing a registry snapshot or a deployment plan is the compiler's, at
step 9.
"""

from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from schema.blueprint import BLUEPRINT_SCHEMA
from schema.catalog import CREW_TEMPLATES, ROLE_TEMPLATES
from schema.tools import KNOWN_TOOLS, PLATFORM_ONLY


class ValidationError(Exception):
    """One or more blueprint problems, rendered for the author."""

    def __init__(self, problems: list[str]) -> None:
        self.problems = problems
        body = "\n".join(f"  - {p}" for p in problems)
        super().__init__(f"blueprint is invalid ({len(problems)} problem(s)):\n{body}")


def _path(parts) -> str:
    out = ""
    for part in parts:
        out += f"[{part}]" if isinstance(part, int) else (f".{part}" if out else part)
    return out or "(document root)"


def _schema_problems(document: Any) -> list[str]:
    validator = Draft202012Validator(BLUEPRINT_SCHEMA)
    problems = []
    for error in sorted(validator.iter_errors(document), key=lambda e: list(e.path)):
        where = _path(error.absolute_path)
        match error.validator:
            case "required":
                problems.append(f"{where}: {error.message}")
            case "additionalProperties":
                problems.append(
                    f"{where}: {error.message} — check the spelling, or the "
                    f"section it belongs to"
                )
            case "enum":
                allowed = ", ".join(repr(v) for v in error.validator_value)
                problems.append(f"{where}: {error.instance!r} is not one of {allowed}")
            case _:
                problems.append(f"{where}: {error.message}")
    return problems


def _catalog_problems(document: Any) -> list[str]:
    """Cross-checks against the versioned catalogs.

    Kept narrow on purpose: only what a *document* can be wrong about
    regardless of cluster state. Workflow-node binding, budget coverage and
    the lean-roster stance are compiler checks (steps 9 and 19).
    """
    problems = []
    known_roles = ", ".join(sorted(ROLE_TEMPLATES))
    known_crews = ", ".join(sorted(CREW_TEMPLATES))

    roles = (document.get("departments") or {}).get("roles") or {}
    for hire, role in roles.items() if isinstance(roles, dict) else []:
        if not isinstance(role, dict) or not isinstance(role.get("template"), str):
            continue  # the schema pass reports the malformed value
        template = ROLE_TEMPLATES.get(role["template"])
        if template is None:
            problems.append(
                f"departments.roles.{hire}.template: "
                f"{role.get('template')!r} is not in the role catalog "
                f"(known: {known_roles})"
            )
            continue
        if template.placeholder_only and role.get("count", 0) != 0:
            problems.append(
                f"departments.roles.{hire}.count: template "
                f"{template.name!r} is a placeholder documenting a growth "
                f"path and is valid only at count: 0"
            )
        for crew in (role.get("may_spawn") or {}).get("crew") or []:
            if crew not in CREW_TEMPLATES:
                problems.append(
                    f"departments.roles.{hire}.may_spawn.crew: {crew!r} is "
                    f"not a known crew template (known: {known_crews})"
                )

    crews = document.get("crews") or {}
    for crew_name, crew in crews.items() if isinstance(crews, dict) else []:
        if not isinstance(crew, dict):
            continue
        members = crew.get("members")
        for member_name, member in (members if isinstance(members, dict) else {}).items():
            if not isinstance(member, dict):
                continue
            if isinstance(member.get("role_template"), str) and (
                member["role_template"] not in ROLE_TEMPLATES
            ):
                problems.append(
                    f"crews.{crew_name}.members.{member_name}.role_template: "
                    f"{member.get('role_template')!r} is not in the role "
                    f"catalog (known: {known_roles})"
                )
            problems += _platform_tool_problems(
                f"crews.{crew_name}.members.{member_name}.tools",
                member.get("tools") or [],
            )

    tools = document.get("tools") or {}
    for role_name, scope in tools.items() if isinstance(tools, dict) else []:
        if isinstance(scope, dict):
            problems += _platform_tool_problems(
                f"tools.{role_name}.allow", scope.get("allow") or []
            )

    return problems


def _platform_tool_problems(where: str, declared: list[str]) -> list[str]:
    """No company role may hold a platform-only tool: `git.merge_pr` belongs
    to the orchestrator identity after the gate (O-1 A), and a blueprint
    able to grant it would make the merge gate optional."""
    return [
        f"{where}: {tool!r} is callable only by the platform identity "
        f"(the orchestrator, after the merge gate) and cannot be granted "
        f"to a company role"
        for tool in declared
        if tool in PLATFORM_ONLY
    ]


def validate_blueprint(document: Any) -> dict[str, Any]:
    """Validate a parsed blueprint; return it, or raise ValidationError with
    every problem found."""
    if not isinstance(document, dict):
        raise ValidationError(
            [f"(document root): expected a YAML mapping, got {type(document).__name__}"]
        )
    # Both passes always run: a typo in `company` should not hide a bad role
    # template three sections away. The catalog checks tolerate malformed
    # values (the schema pass reports those) and simply skip what they
    # cannot read.
    problems = _schema_problems(document) + _catalog_problems(document)
    if problems:
        raise ValidationError(problems)
    return document


def load_blueprint(path: Path | str) -> dict[str, Any]:
    """Parse and validate a blueprint file."""
    text = Path(path).read_text()
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValidationError([f"(document root): not valid YAML — {exc}"]) from exc
    return validate_blueprint(document)


__all__ = [
    "BLUEPRINT_SCHEMA",
    "KNOWN_TOOLS",
    "ValidationError",
    "load_blueprint",
    "validate_blueprint",
]
