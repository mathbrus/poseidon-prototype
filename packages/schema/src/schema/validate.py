"""Blueprint validation with errors a human can act on.

`jsonschema`'s default messages name a failing keyword deep in a combined
schema; what an author needs is *where* in their document and *what to
write instead*. So every error is rendered as a YAML-ish path plus the
concrete allowed values, and errors are reported together — fixing a
blueprint one message per run is a bad trade for the author.

Structural checks only. The catalog cross-checks (does this template exist,
does an ephemeral agent's role match the catalog's) live here too, because they
are properties of the *document* against a versioned catalog; anything
needing a registry snapshot or a deployment plan is the compiler's.
"""

from pathlib import Path
from typing import Any

import yaml
from jsonschema import Draft202012Validator

from schema.blueprint import BLUEPRINT_SCHEMA
from schema.catalog import CREW_TEMPLATES, ROLE_TEMPLATES
from schema.coherence import (
    contract_role_problems,
    crew_problems,
    platform_tool_problems,
    role_template_problems,
)
from schema.gates import contract_tests_problems, merge_gate_problems
from schema.tools import KNOWN_TOOLS


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
    """Cross-checks against the versioned catalogs (`schema.coherence`).

    Kept narrow on purpose: only what a *document* can be wrong about
    regardless of cluster state. Workflow-node binding, budget coverage and
    the lean-roster stance are compiler checks.
    """
    problems = role_template_problems(document, ROLE_TEMPLATES, CREW_TEMPLATES)
    # Unknown tool names are the schema enum's to report, so the shared
    # check does not repeat them here.
    problems += crew_problems(
        document, ROLE_TEMPLATES, CREW_TEMPLATES, report_unknown_tools=False
    )
    tools = document.get("tools") or {}
    for role_name, scope in tools.items() if isinstance(tools, dict) else []:
        if isinstance(scope, dict):
            problems += platform_tool_problems(
                f"tools.{role_name}.allow", scope.get("allow") or []
            )
    return problems + contract_role_problems(document, ROLE_TEMPLATES)


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
    problems = (
        _schema_problems(document)
        + _catalog_problems(document)
        + merge_gate_problems(document)
        + contract_tests_problems(document)
    )
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
