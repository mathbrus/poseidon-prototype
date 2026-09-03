"""JSON Schema for the eight blueprint sections.

Structural validation only. Cross-checks that need the catalogs or a
registry snapshot (does this template exist, does this workflow node bind to
an active conductor, do budgets cover enabled workflows) belong to the
compiler — the schema's job is to make a document
well-formed, not to make a company coherent.

One convention worth stating once: every object is `additionalProperties:
false`, so a typo'd key is an error rather than a silently ignored line. The
lean-standing stance on `count` is the compiler's to enforce, not the
schema's.
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
    },
}

# The company's subject — the one repository every run works on and the
# branch its changes are measured and delivered against — plus the sandbox
# image, test command and egress allowlist the infra IR is built from (an
# empty allowlist means zero egress).
_STACK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo"],
    "properties": {
        "repo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url", "base_ref"],
            "properties": {"url": _TEXT, "base_ref": _TEXT},
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

_HIRE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role", "count"],
    "properties": {
        "role": _NAME,
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

# The standing agents, keyed by the hire's name so a blueprint diff
# (hire/fire) is readable; one entry can carry several instances (`count`).
_STANDING = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": _HIRE,
    "propertyNames": _NAME,
}

# Per-role tool scopes → gateway allow rules, for standing and ephemeral
# agents of the role alike. A scope narrows the catalog role's tools and
# never widens them.
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


# One gate shape: a gate names its resolver and how long it waits for a
# human (escalation fires at half of that, expiry at the end), and differs
# only in which resolvers it allows and what a resolver reads. A gate
# nobody declares is a stop nobody takes — for merge that means the review
# ends without merging at all, an unattended auto-merge being no default
# anyone gets. The deadline is explicit because a defaulted wait is
# patience nobody wrote down.
def _gate(resolvers: list[str], extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["resolved_by", "deadline_minutes"],
        "properties": {
            "resolved_by": {"type": "string", "enum": resolvers},
            "deadline_minutes": {"type": "integer", "minimum": 1},
            **(extra or {}),
        },
    }


# The delegated class a policy-resolved merge may finish on its own:
# explicit paths and an explicit rework bound, all-explicit like
# `heartbeat` — a defaulted bound is authority nobody wrote down. The
# undelegatable carve-outs live in `schema.gates` and are refused at
# validate time, whatever the paths here say.
_MERGE_DELEGATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paths", "max_reworks"],
    "properties": {
        "paths": {"type": "array", "items": _TEXT, "minItems": 1, "uniqueItems": True},
        "max_reworks": {"type": "integer", "minimum": 0},
    },
}

_INTAKE_GATE = _gate(["human"])
_MERGE_GATE = _gate(["human", "policy"], {"delegate": _MERGE_DELEGATION})


def _workflow(gates: dict[str, Any] | None) -> dict[str, Any]:
    """One workflow node. Each names only the gates it actually parks on —
    a merge gate on intake was never a meaningful thing to write, so it is
    not a writable thing."""
    properties: dict[str, Any] = {
        "enabled": {"type": "boolean"},
        # The standing name that spawns this workflow — never a standing
        # implementer (enforced by the compiler).
        "conductor": _NAME,
        "crew": _NAME,
        # Bounded verify re-dispatches.
        "max_verify_retries": {"type": "integer", "minimum": 0},
    }
    if gates:
        properties["gates"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": gates,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["enabled", "conductor"],
        "properties": properties,
    }


_WORKFLOWS = {
    "type": "object",
    "additionalProperties": False,
    "minProperties": 1,
    "properties": {
        "intake": _workflow({"intake": _INTAKE_GATE}),
        "implement": _workflow(None),
        "review": _workflow({"merge": _MERGE_GATE}),
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
        # The contract tree: the contract/acceptance tests and the role
        # templates that may deliver into it. Compiles to a `git.open_pr`
        # deny for every other role and joins the merge carve-outs — the
        # code that certifies a change never auto-merges with it, and the
        # implementer cannot rewrite the spec its own work is graded by.
        "contract_tests": {
            "type": "object",
            "additionalProperties": False,
            "required": ["paths", "roles"],
            "properties": {
                "paths": {
                    "type": "array",
                    "items": _TEXT,
                    "minItems": 1,
                    "uniqueItems": True,
                },
                "roles": {
                    "type": "array",
                    "items": _NAME,
                    "minItems": 1,
                    "uniqueItems": True,
                },
            },
        },
    },
}

# The standing heartbeat's declared authority: how stale its view of the
# world may grow, concurrency, what it may start on its own initiative,
# and a daily hard stop. Opt-in and
# all-explicit — a company that declares a heartbeat declares every cap,
# because a defaulted cap is authority nobody wrote down. Absent means what
# it always meant: runs start only when a human starts one. `intake` is
# deliberately not startable — goals enter through a human.
_HEARTBEAT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "conductor",
        "reconcile_minutes",
        "max_concurrent_runs",
        "max_attempts",
        "may_start",
        "daily_cap",
    ],
    "properties": {
        # The standing name dispatched to decide the next action.
        "conductor": _NAME,
        # The reconcile backstop: the platform's own completions wake the
        # tick immediately; this bounds how long a change only the outside
        # world saw (an issue filed by hand, a PR merged by hand) can wait.
        "reconcile_minutes": {"type": "integer", "minimum": 1},
        "max_concurrent_runs": {"type": "integer", "minimum": 1},
        # How many runs the heartbeat may start on one work item — an
        # issue or a PR — before it stops offering it. A run that
        # concluded (delivered, or reached a verdict) consumes the item
        # at once; this bounds the aborted ones (a timed-out model call,
        # a step cap, a cancel). 1 means every run is the only try.
        "max_attempts": {"type": "integer", "minimum": 1},
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
        # `usd` is the hard stop at the LLM gateway; `pause_minutes` is how
        # long a run stopped by it waits for a human to raise the cap
        # (escalation at half, then the run fails).
        "per_run": {
            "type": "object",
            "additionalProperties": False,
            "required": ["usd", "pause_minutes"],
            "properties": {
                "usd": {"type": "number", "exclusiveMinimum": 0},
                "pause_minutes": {"type": "integer", "minimum": 1},
            },
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
        "standing",
        "tools",
        "workflows",
        "budgets",
    ],
    "properties": {
        "apiVersion": {"type": "string", "enum": ["poseidon.dev/v1"]},
        "company": _COMPANY,
        "stack": _STACK,
        "standing": _STANDING,
        "tools": _TOOLS,
        "workflows": _WORKFLOWS,
        "policies": _POLICIES,
        "budgets": _BUDGETS,
        "heartbeat": _HEARTBEAT,
    },
}
