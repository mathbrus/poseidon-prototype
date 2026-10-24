"""JSON Schema for the eight blueprint sections.

Structural validation only. Cross-checks that need the catalogs or a
registry snapshot (does this template exist, is the conductor an active
standing spawner, can the daily cap fund a run) belong to the
compiler — the schema's job is to make a document
well-formed, not to make a company coherent.

Every object is `additionalProperties: false`, so a typo'd key is an
error rather than a silently ignored line. Every field carries a
`description`: the docs site renders the blueprint reference from it, and
the build fails on a field without one. No field has a default — a
defaulted cap or bound is authority nobody wrote down.
"""

from typing import Any

from blueprint.documents import DOCUMENT_LABELS, DOCUMENT_NAMES
from blueprint.tools import KNOWN_TOOLS

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

# A path inside the company's repository, relative to its root.
_REPO_PATH = {"type": "string", "minLength": 1, "pattern": r"^[^\s/][^\s]*$"}


def _described(node: dict[str, Any], description: str) -> dict[str, Any]:
    """A shared field shape with this use's description."""
    return {**node, "description": description}


def _paths(description: str) -> dict[str, Any]:
    return {
        "type": "array",
        "items": _REPO_PATH,
        "minItems": 1,
        "uniqueItems": True,
        "description": description,
    }


# Humans are required because every company has at least one gate only a
# person can answer: the budget park, and an automated merge falls back
# to one. The vision's cap keeps it a statement of purpose, not a
# specification.
_COMPANY = {
    "type": "object",
    "additionalProperties": False,
    "required": ["name", "vision", "humans", "documents"],
    "description": "Who the company is, what it is for, and who may act on it.",
    "properties": {
        "name": _described(
            _NAME,
            "The company's name. It keys everything the platform records about"
            " the company and names its policy package. A cluster hosts one"
            " company.",
        ),
        "vision": {
            "type": "string",
            "minLength": 1,
            "maxLength": 4000,
            "description": "What the company builds and why, in your words. Every"
            " agent reads it in its system prompt. Only applying the blueprint"
            " again changes it.",
        },
        "humans": {
            "type": "array",
            "items": _HUMAN,
            "minItems": 1,
            "uniqueItems": True,
            "description": "The people who may act on the company through the"
            " console: answer its gates, start or cancel runs, pause, resume or"
            " wake the heartbeat, apply the blueprint, delete the company. On"
            " kind, only the people the chart declares (`owner`, `auditor`).",
        },
        "documents": {
            "type": "object",
            "additionalProperties": False,
            "required": list(DOCUMENT_NAMES),
            "description": "Where, in the repository, the company keeps the two"
            " documents it writes for itself. A document missing on `base_ref`"
            " is what the first heartbeat ticks write.",
            "properties": {
                name: _described(_REPO_PATH, f"Path of the {DOCUMENT_LABELS[name]}.")
                for name in DOCUMENT_NAMES
            },
        },
    },
}

_STACK = {
    "type": "object",
    "additionalProperties": False,
    "required": ["repo", "toolchain"],
    "description": "The repository the company works on, and the sandbox its"
    " code runs in.",
    "properties": {
        "repo": {
            "type": "object",
            "additionalProperties": False,
            "required": ["url", "base_ref"],
            "description": "The one repository every run works on.",
            "properties": {
                "url": _described(
                    _TEXT,
                    "The repository, as `github.com/<owner>/<name>`. Policy"
                    " refuses a tool call naming any other repository"
                    " (`repo_not_declared`).",
                ),
                "base_ref": _described(
                    _TEXT,
                    "The branch changes are diffed and delivered against, and the"
                    " documents are read from.",
                ),
            },
        },
        "toolchain": {
            "type": "object",
            "additionalProperties": False,
            "required": ["image", "test_command"],
            "description": "The sandbox every run's code runs in.",
            "properties": {
                "image": _described(
                    _TEXT,
                    "The sandbox image. `poseidon-sandbox` is the platform's own"
                    " Python toolchain; any other image is used as written.",
                ),
                "test_command": _described(
                    _TEXT,
                    "The command the platform runs to decide whether the tests"
                    " pass, for example `pytest -q`.",
                ),
                "egress_allow": {
                    "type": "array",
                    "items": _TEXT,
                    "uniqueItems": True,
                    "description": "Every host a sandbox may reach, through the"
                    " egress proxy. Absent or empty means no egress at all.",
                },
            },
        },
    },
}

_HIRE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["role", "count"],
    "description": "One standing agent, keyed by its name.",
    "properties": {
        "role": _described(
            _NAME,
            "The catalog role it holds. A standing agent must be a conductor:"
            " a role that may start crews, such as `pm`.",
        ),
        "count": {
            "type": "integer",
            "enum": [0, 1],
            "description": "`1` runs the agent. `0` declares it without running it.",
        },
        "may_spawn": {
            "type": "object",
            "additionalProperties": False,
            "description": "The crews this agent may start, and how many at once.",
            "properties": {
                "crew": {
                    "type": "array",
                    "items": _NAME,
                    "uniqueItems": True,
                    "description": "Crew names from the catalog. Every workflow's"
                    " crew must be on this list.",
                },
                "max_concurrent": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How many of its crews may run at once. Policy"
                    " checks it against a live count.",
                },
            },
        },
    },
}

_STANDING = {
    "type": "object",
    "minProperties": 1,
    "additionalProperties": _HIRE,
    "propertyNames": _NAME,
    "description": "The standing agents, keyed by name. They live as long as the"
    " company. Agents that write code are always ephemeral, so a standing"
    " `implementer` doesn't compile.",
}

_TOOLS = {
    "type": "object",
    "additionalProperties": {
        "type": "object",
        "additionalProperties": False,
        "required": ["allow"],
        "description": "One role's tool scope, keyed by role name.",
        "properties": {
            "allow": {
                "type": "array",
                "items": {"type": "string", "enum": sorted(KNOWN_TOOLS)},
                "uniqueItems": True,
                "description": "The tools agents of this role may call. It can"
                " narrow the role's catalog tools, never widen them.",
            }
        },
    },
    "propertyNames": _NAME,
    "description": "Narrows a role's tools, for every agent of that role. A role"
    " not listed keeps its catalog tools.",
}


# What policy may merge without a person's verdict. The protected surfaces
# that can never be automated live in `blueprint.gates` and are refused at
# validate time.
_MERGE_AUTOMATION = {
    "type": "object",
    "additionalProperties": False,
    "required": ["paths", "max_reworks"],
    "description": "What policy may merge without a person's verdict. Without"
    " it, every merge waits for one.",
    "properties": {
        "paths": _paths(
            "The paths a pull request may change and still merge without a"
            " person. May not overlap `.github/`, `policies.deny_paths` or the"
            " contract tree."
        ),
        "max_reworks": {
            "type": "integer",
            "minimum": 0,
            "description": "How many rework tickets a failed review may file"
            " before the pull request parks on a person.",
        },
    },
}

# A gate nobody declares is a gate nobody passes.
_MERGE_GATE = {
    "type": "object",
    "additionalProperties": False,
    "required": ["deadline_minutes"],
    "description": "Whether a reviewed pull request merges. Without it, a"
    " review ends without merging.",
    "properties": {
        "deadline_minutes": {
            "type": "integer",
            "minimum": 1,
            "description": "How long the gate waits for a person. It"
            " escalates at half and expires at the end.",
        },
        "automate": _MERGE_AUTOMATION,
    },
}


def _workflow(
    description: str,
    gates: dict[str, Any] | None,
    limits: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """One crewed workflow node: the crew that fulfils its runs, the limits
    it reads and the gates it parks on — nothing else. Which workflows the
    company runs is `heartbeat.may_start`; who conducts them is
    `heartbeat.conductor`; each is said once. A merge gate on implement,
    or a verify limit on review, was never a meaningful thing to write, so
    it is not a writable thing. Limits are required."""
    properties: dict[str, Any] = {
        "crew": _described(_NAME, "The crew from the catalog that does the run."),
        **(limits or {}),
    }
    if gates:
        properties["gates"] = {
            "type": "object",
            "additionalProperties": False,
            "description": "The gates the run parks on.",
            "properties": gates,
        }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["crew", *(limits or {})],
        "description": description,
        "properties": properties,
    }


# `scope` has no node: it dispatches the conductor directly and configures
# nothing.
_WORKFLOWS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["implement", "review"],
    "description": "The two workflows that put a crew to work.",
    "properties": {
        "implement": _workflow(
            "Turns a ticket into a pull request.",
            None,
            {
                "max_verify_retries": {
                    "type": "integer",
                    "minimum": 0,
                    "description": "How many times the platform sends the"
                    " implementer back to work, after a failed test run or a"
                    " delivery policy refused, before the run fails.",
                }
            },
        ),
        "review": _workflow(
            "Reviews a delivered pull request and decides whether it merges.",
            {"merge": _MERGE_GATE},
        ),
    },
}

_POLICIES = {
    "type": "object",
    "additionalProperties": False,
    "description": "Rules on what a delivery may change. Each compiles to a deny"
    " rule the gateway checks against the real changed paths.",
    "properties": {
        "branch_protection": {
            "type": "object",
            "additionalProperties": False,
            "description": "Which branches a delivery may target.",
            "properties": {
                "protected": {
                    "type": "array",
                    "items": _TEXT,
                    "uniqueItems": True,
                    "description": "Branches no delivery may target.",
                },
                "allowed_pattern": _described(
                    _TEXT,
                    "A regular expression every delivery branch must match.",
                ),
            },
        },
        "deny_paths": {
            "type": "array",
            "items": _REPO_PATH,
            "uniqueItems": True,
            "description": "Paths no delivery may change, for example"
            " `.github/workflows/`.",
        },
        "contract_tests": {
            "type": "object",
            "additionalProperties": False,
            "required": ["paths", "roles"],
            "description": "Acceptance tests the implementer can't rewrite."
            " Automated merges are graded against this tree as it stands on"
            " `base_ref`, and changes to it always need a person.",
            "properties": {
                "paths": _paths("The contract tree, for example `tests/contract/`."),
                "roles": {
                    "type": "array",
                    "items": _NAME,
                    "minItems": 1,
                    "uniqueItems": True,
                    "description": "The roles that may deliver into the tree,"
                    " for example `contract-author`.",
                },
            },
        },
    },
}

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
    "description": "The company's own initiative: who decides what to do next,"
    " and the caps on what it may start. Every company has one.",
    "properties": {
        "conductor": _described(
            _NAME,
            "The standing agent that decides each tick and plans the work. Every"
            " crew is started under its authority.",
        ),
        "reconcile_minutes": {
            "type": "integer",
            "minimum": 1,
            "description": "The longest the heartbeat sleeps. It also wakes"
            " whenever one of the company's runs ends.",
        },
        "max_concurrent_runs": {
            "type": "integer",
            "minimum": 1,
            "description": "How many of the company's runs may be in flight at once.",
        },
        "max_attempts": {
            "type": "integer",
            "minimum": 1,
            "description": "How many runs may take up one ticket or pull request"
            " that keeps failing before the heartbeat stops offering it. `1`"
            " means one try.",
        },
        "max_open_tickets": {
            "type": "integer",
            "minimum": 1,
            "description": "The heartbeat won't plan more work while the"
            " repository has this many open issues.",
        },
        "may_start": {
            "type": "array",
            "items": {"type": "string", "enum": ["implement", "review", "scope"]},
            "minItems": 1,
            "uniqueItems": True,
            "description": "The workflows the heartbeat may start on its own. Leave"
            " one out and only a person starts it.",
        },
        "daily_cap": {
            "type": "object",
            "additionalProperties": False,
            "required": ["usd"],
            "description": "Spend per calendar day after which the heartbeat"
            " starts no run.",
            "properties": {
                "usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "The cap, in US dollars. It must fund at least"
                    " one run's `budgets.per_run.usd`.",
                }
            },
        },
    },
}

_BUDGETS = {
    "type": "object",
    "additionalProperties": False,
    "required": ["per_run"],
    "description": "What one run may spend on model calls.",
    "properties": {
        "per_run": {
            "type": "object",
            "additionalProperties": False,
            "required": ["usd", "deadline_minutes"],
            "description": "The cap on each run.",
            "properties": {
                "usd": {
                    "type": "number",
                    "exclusiveMinimum": 0,
                    "description": "The cap, in US dollars. At the cap the LLM"
                    " gateway refuses further calls and the run parks for a"
                    " person.",
                },
                "deadline_minutes": {
                    "type": "integer",
                    "minimum": 1,
                    "description": "How long a run parked at its cap waits for a"
                    " verdict. It escalates at half, and the run fails at the end.",
                },
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
        "apiVersion": {
            "type": "string",
            "enum": ["poseidon.dev/v1"],
            "description": "The blueprint format's version.",
        },
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
