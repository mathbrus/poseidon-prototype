"""JSON Schema for the eight blueprint sections.

Structural validation only. Cross-checks that need the catalogs or a
registry snapshot (does this template exist, is the conductor an active
standing spawner, can the daily cap fund a run) belong to the
compiler — the schema's job is to make a document
well-formed, not to make a company coherent.

One convention worth stating once: every object is `additionalProperties:
false`, so a typo'd key is an error rather than a silently ignored line. The
lean-standing stance on `count` is the compiler's to enforce, not the
schema's.
"""

from typing import Any

from schema.documents import DOCUMENT_NAMES
from schema.tools import KNOWN_TOOLS

SCHEMA_ID = "https://poseidon.dev/schemas/blueprint/v1"

_NAME = {"type": "string", "minLength": 1, "pattern": r"^[a-z0-9][a-z0-9-]*$"}
# A human's name: what the console verifies them as — a chart-declared
# `human-<name>` on kind, or an identity provider's username elsewhere.
_HUMAN = {
    "type": "string",
    "minLength": 1,
    "maxLength": 254,
    "pattern": r"^[A-Za-z0-9][A-Za-z0-9._@:-]*$",
}
_TEXT = {"type": "string", "minLength": 1}
_TOOL_LIST = {
    "type": "array",
    "items": {"type": "string", "enum": sorted(KNOWN_TOOLS)},
    "uniqueItems": True,
}

# A path inside the company's repository, relative to its root.
_REPO_PATH = {"type": "string", "minLength": 1, "pattern": r"^[^\s/][^\s]*$"}

# The company's name, its vision — the one text a human writes — the
# humans who may act on it, and the paths of the two documents the
# company writes for itself. The humans are the people the console lets
# resolve a gate, start or cancel a run by hand, wake the heartbeat and
# re-apply this document: named here so that authority is a diff, and
# required because every company has at least one gate only a person can
# answer (the budget pause; a policy-resolved merge falls back to one).
# The vision
# reaches every procedure but `deliver` through the static prompt and
# changes only when the blueprint is applied again; the cap keeps it a
# charter, not a specification. The documents live in the repository at
# `base_ref`: the strategy the steward writes once from the vision, and
# the architecture masterplan it writes at bootstrap and revises on
# request. Absence on `base_ref` is not a schema matter — it is what the
# first ticks are for.
_COMPANY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "vision", "humans", "documents"],
    "properties": {
        "name": _NAME,
        "vision": {"type": "string", "minLength": 1, "maxLength": 4000},
        "humans": {
            "type": "array",
            "items": _HUMAN,
            "minItems": 1,
            "uniqueItems": True,
        },
        "documents": {
            "type": "object",
            "additionalProperties": False,
            "required": list(DOCUMENT_NAMES),
            "properties": {name: _REPO_PATH for name in DOCUMENT_NAMES},
        },
    },
}

# The company's subject — the one repository every run works on and the
# branch its changes are measured and delivered against — plus the sandbox
# image, test command and egress allowlist the infra IR is built from (an
# empty allowlist means zero egress).
_STACK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo", "toolchain"],
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
            "required": ["image", "test_command"],
            "properties": {
                # `poseidon-sandbox` names the platform's own toolchain, which
                # the deployment resolves to its content-addressed image;
                # any other image is used as written.
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
        # One instance per hire (the operator names one Deployment and one
        # Service per standing name); 0 documents a growth path.
        "count": {"type": "integer", "enum": [0, 1]},
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
        "paths": {
            "type": "array",
            "items": _REPO_PATH,
            "minItems": 1,
            "uniqueItems": True,
        },
        "max_reworks": {"type": "integer", "minimum": 0},
    },
}

_MERGE_GATE = _gate(["human", "policy"], {"delegate": _MERGE_DELEGATION})


def _workflow(
    gates: dict[str, Any] | None, bounds: dict[str, Any] | None = None
) -> dict[str, Any]:
    """One crewed workflow node: the crew that fulfils its runs, the bounds
    it reads and the gates it parks on — nothing else. Which workflows the
    company runs is `heartbeat.may_start`; who conducts them is
    `heartbeat.conductor`; each is said once. A merge gate on implement,
    or a verify bound on review, was never a meaningful thing to write, so
    it is not a writable thing. Bounds are required: a defaulted bound is
    authority nobody wrote down."""
    properties: dict[str, Any] = {"crew": _NAME, **(bounds or {})}
    if gates:
        properties["gates"] = {
            "type": "object",
            "additionalProperties": False,
            "properties": gates,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["crew", *(bounds or {})],
        "properties": properties,
    }


# The two workflows that put a crew to work. `scope` has no node: it
# dispatches the conductor directly and configures nothing.
_WORKFLOWS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["implement", "review"],
    "properties": {
        # How many times a red platform verify re-dispatches the implementer
        # before the run fails.
        "implement": _workflow(
            None, {"max_verify_retries": {"type": "integer", "minimum": 0}}
        ),
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
        "deny_paths": {"type": "array", "items": _REPO_PATH, "uniqueItems": True},
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
                    "items": _REPO_PATH,
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

# The standing heartbeat: the company's own initiative. Every company has
# one — work originates nowhere else — and it is all-explicit: who decides,
# how stale its view of the world may grow, concurrency, how many tries an
# item gets, how large the backlog may grow before scoping is refused, what
# it may start, and a daily hard stop. A defaulted cap is authority nobody
# wrote down.
_HEARTBEAT = {
    "type": "object",
    "additionalProperties": False,
    "required": [
        "conductor",
        "reconcile_minutes",
        "max_concurrent_runs",
        "max_attempts",
        "max_open_tickets",
        "may_start",
        "daily_cap",
    ],
    "properties": {
        # The one standing agent the company's runs are requested under:
        # dispatched to decide each tick and to scope, and the authority
        # every crew is fulfilled under.
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
        # `scope` is refused while the repository has this many open
        # issues or more — every open issue counts, worked or not — so
        # the conductor cannot flood its own backlog tick after tick.
        "max_open_tickets": {"type": "integer", "minimum": 1},
        # What the heartbeat may start on its own: scoping the next slice
        # from the company's documents, implementing a ticket, reviewing
        # a delivered pull request.
        "may_start": {
            "type": "array",
            "items": {"type": "string", "enum": ["implement", "review", "scope"]},
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
        "heartbeat",
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
