"""JSON Schema for the nine blueprint sections.

Structural validation only. Cross-checks that need the catalogs or a
registry snapshot (does this template exist, does this workflow node bind to
a hired conductor, do budgets cover enabled workflows) belong to the
compiler — the schema's job is to make a document
well-formed, not to make a company coherent.

Two conventions worth stating once: every object is `additionalProperties:
false`, so a typo'd key is an error rather than a silently ignored line; and
`count` is where the lean-roster stance will be enforced, but only the
`analyst` placeholder is capped structurally here (see catalog.py).
"""

from typing import Any

from schema.catalog import MODEL_TIERS
from schema.tools import KNOWN_TOOLS

SCHEMA_ID = "https://poseidon.dev/schemas/blueprint/v1"

_NAME = {"type": "string", "minLength": 1, "pattern": r"^[a-z0-9][a-z0-9-]*$"}
_TEXT = {"type": "string", "minLength": 1}
_TOOL_LIST = {
    "type": "array",
    "items": {"type": "string", "enum": sorted(KNOWN_TOOLS)},
    "uniqueItems": True,
}

_COMPANY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "mission"],
    "properties": {
        "name": _NAME,
        "mission": _TEXT,
        "product": _TEXT,
        "stage": {"type": "string", "enum": ["idea", "prototype", "growth"]},
    },
}

# Feeds the sandbox image and the egress allowlist through the infra IR;
# an empty allowlist means zero egress.
_STACK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["languages", "repos"],
    "properties": {
        "languages": {"type": "array", "items": _TEXT, "minItems": 1},
        "frameworks": {"type": "array", "items": _TEXT},
        "repos": {
            "type": "array",
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "url"],
                "properties": {
                    "name": _NAME,
                    "url": _TEXT,
                    "default_branch": {"type": "string", "default": "main"},
                },
            },
        },
        "toolchain": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "image": _TEXT,
                "test_command": _TEXT,
                "egress_allow": {"type": "array", "items": _TEXT, "uniqueItems": True},
            },
        },
    },
}

_ROLE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["template", "count"],
    "properties": {
        "template": _NAME,
        "count": {"type": "integer", "minimum": 0},
        "model_tier": {"type": "string", "enum": list(MODEL_TIERS)},
        "may_spawn": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "crew": {"type": "array", "items": _NAME, "uniqueItems": True},
                "max_concurrent": {"type": "integer", "minimum": 1},
            },
        },
    },
}

_DEPARTMENTS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["roles"],
    "properties": {
        # Keyed by hire name so a blueprint diff (hire/fire) is readable.
        "roles": {
            "type": "object",
            "minProperties": 1,
            "additionalProperties": _ROLE,
            "propertyNames": _NAME,
        }
    },
}

_CREW_MEMBER = {
    "type": "object",
    "additionalProperties": False,
    # The allow-list is explicit per member, always: an implicit scope is
    # what the compiler coherence check refuses to guess.
    "required": ["role_template", "tools"],
    "properties": {
        "role_template": _NAME,
        "tools": _TOOL_LIST,
        "needs_sandbox": {"type": "boolean"},
    },
}

_CREWS = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": False,
        "required": ["members"],
        "properties": {
            "members": {
                "type": "object",
                "minProperties": 1,
                "additionalProperties": _CREW_MEMBER,
                "propertyNames": _NAME,
            },
            "budget_inheritance": {"type": "string", "enum": ["run"]},
        },
    },
    "propertyNames": _NAME,
}

# Per-role method-level scopes → gateway allow rules.
_TOOLS = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": False,
        "required": ["allow"],
        "properties": {"allow": _TOOL_LIST},
    },
    "propertyNames": _NAME,
}

_GATE = {"type": "string", "enum": ["none", "human_approval"]}

_WORKFLOW = {
    "type": "object",
    "additionalProperties": False,
    "required": ["enabled", "conductor"],
    "properties": {
        "enabled": {"type": "boolean"},
        # The standing role that spawns/conducts this workflow — never a
        # standing executor (enforced by the compiler).
        "conductor": _NAME,
        "crew": _NAME,
        "gates": {
            "type": "object",
            "additionalProperties": False,
            "properties": {"merge": _GATE, "intake": _GATE},
        },
        # Bounded verify re-dispatches.
        "max_verify_retries": {"type": "integer", "minimum": 0},
        "drain": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                # What happens to in-flight runs when the conductor is
                # drained by a blueprint diff.
                "on_conductor_drain": {
                    "type": "string",
                    "enum": ["abort_crew", "finish_run"],
                },
                "timeout_minutes": {"type": "integer", "minimum": 1},
            },
        },
    },
}

_WORKFLOWS = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "intake": _WORKFLOW,
        "implement": _WORKFLOW,
        "review": _WORKFLOW,
    },
}

_POLICIES = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "branch_protection": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "protected": {"type": "array", "items": _TEXT, "uniqueItems": True},
                "allowed_pattern": _TEXT,
            },
        },
        "deny_paths": {"type": "array", "items": _TEXT, "uniqueItems": True},
        "path_ownership": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["paths", "roles"],
                "properties": {
                    "paths": {"type": "array", "items": _TEXT, "minItems": 1},
                    # Role templates, not instance ids: path rules name what
                    # a caller *is*.
                    "roles": {"type": "array", "items": _NAME, "minItems": 1},
                },
            },
        },
        "escalation": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "channel": {"type": "string", "enum": ["api"]},
                "timeout_minutes": {"type": "integer", "minimum": 1},
            },
        },
    },
}

# The standing heartbeat's declared authority: cadence, concurrency, what
# it may start on its own initiative, and a daily hard stop. Opt-in and
# all-explicit — a company that declares a heartbeat declares every cap,
# because a defaulted cap is authority nobody wrote down. Absent means what
# it always meant: runs start only when a human starts one. `intake` is
# deliberately not startable — goals enter through a human.
_CONDUCT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "conductor",
        "heartbeat_minutes",
        "max_concurrent_runs",
        "may_start",
        "daily_cap",
    ],
    "properties": {
        # The standing role dispatched to decide the next action.
        "conductor": _NAME,
        "heartbeat_minutes": {"type": "integer", "minimum": 1},
        "max_concurrent_runs": {"type": "integer", "minimum": 1},
        "may_start": {
            "type": "array",
            "items": {"type": "string", "enum": ["implement", "review"]},
            "minItems": 1,
            "uniqueItems": True,
        },
        # Halts *starting*, per calendar day; the per-run hard stop still
        # governs each run it funds.
        "daily_cap": {
            "type": "object",
            "additionalProperties": False,
            "required": ["usd"],
            "properties": {"usd": {"type": "number", "exclusiveMinimum": 0}},
        },
    },
}

_BUDGETS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["per_run"],
    "properties": {
        # Hard-stop at the LLM gateway.
        "per_run": {
            "type": "object",
            "additionalProperties": False,
            "required": ["usd"],
            "properties": {"usd": {"type": "number", "exclusiveMinimum": 0}},
        },
    },
}

BLUEPRINT_SCHEMA: dict[str, Any] = {
    "$schema": "https://json-schema.org/draft/2020-12/schema",
    "$id": SCHEMA_ID,
    "title": "Poseidon company blueprint",
    "type": "object",
    "additionalProperties": False,
    "required": [
        "apiVersion",
        "company",
        "stack",
        "departments",
        "crews",
        "tools",
        "workflows",
        "budgets",
    ],
    "properties": {
        "apiVersion": {"type": "string", "enum": ["poseidon.dev/v1"]},
        "company": _COMPANY,
        "stack": _STACK,
        "departments": _DEPARTMENTS,
        "crews": _CREWS,
        "tools": _TOOLS,
        "workflows": _WORKFLOWS,
        "policies": _POLICIES,
        "budgets": _BUDGETS,
        "conduct": _CONDUCT,
    },
}
