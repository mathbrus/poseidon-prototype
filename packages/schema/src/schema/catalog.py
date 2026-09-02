"""Versioned role-template and crew-template catalogs.

A blueprint names templates; the compiler resolves them into agent specs and
per-agent Rego bundles. Templates carry tool scopes, model tier and runtime
hints — never counts, never secrets: how many of a role a company hires is
the blueprint's business, and credentials never enter a spec at all.

One rule the catalog encodes rather than documents: `backend-engineer` and
`sdet` exist **only** as ephemeral agents. The schema does not forbid
standing counts structurally — the compiler error does.
"""

from dataclasses import dataclass, field

from schema.tools import CREW_TOOLS, ISSUE_TOOLS, REVIEW_TOOLS, SANDBOX_TOOLS

CATALOG_VERSION = "v1"

MODEL_TIERS = ("small", "standard", "frontier")


@dataclass(frozen=True)
class RoleTemplate:
    name: str
    summary: str
    # Tools an instance of this template may call, before the blueprint
    # narrows them further. The compiler emits the intersection.
    tools: tuple[str, ...]
    model_tier: str
    # Crew templates this role may request, when the blueprint grants them.
    may_spawn: tuple[str, ...] = ()
    # Prompt/runtime hints the brain host reads; not policy.
    runtime_hints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class EphemeralAgent:
    role_template: str
    # The agent's explicit allow-list — deliberately NOT required to be a
    # subset of the conductor's scope: spawn authority is not a capability
    # ceiling.
    tools: tuple[str, ...]


@dataclass(frozen=True)
class CrewTemplate:
    name: str
    summary: str
    agents: dict[str, EphemeralAgent]
    # Agents inherit the run's budget; the ledger keeps them as separate
    # dimensions so crew fan-out stays attributable.
    budget_inheritance: str = "run"


ROLE_TEMPLATES: dict[str, RoleTemplate] = {
    "pm": RoleTemplate(
        name="pm",
        summary=(
            "Standing conductor: scopes goals, writes, reads and comments "
            "tickets, requests crews. Holds no executor tools — the PM cannot "
            "open a pull request or write in a sandbox."
        ),
        tools=tuple(ISSUE_TOOLS + CREW_TOOLS),
        model_tier="standard",
        may_spawn=("contract-authoring", "feature-delivery", "feature-review"),
        runtime_hints={
            "memory": "none",  # cross-run state is tickets + registry runs
            "dispatch": "http",
        },
    ),
    "backend-engineer": RoleTemplate(
        name="backend-engineer",
        summary=(
            "Coding executor. Ephemeral agents only; standing counts are a compile error."
        ),
        tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
        model_tier="frontier",
        runtime_hints={"dispatch": "http", "brain": "coding"},
    ),
    "sdet": RoleTemplate(
        name="sdet",
        summary="Reviewer: reads the diff, runs tests, posts findings.",
        tools=tuple(SANDBOX_TOOLS + REVIEW_TOOLS + ["issues.comment"]),
        model_tier="standard",
        runtime_hints={"dispatch": "http", "brain": "review"},
    ),
    # Deliver-capable on purpose, unlike `sdet`: the contract tree is
    # delivered through the same governed `git.open_pr` as any change, and
    # `policies.contract_tests` is what fences who may touch it. A distinct
    # template (not `backend-engineer`) is what lets that fence name the
    # spec's author without naming the implementer.
    "contract-author": RoleTemplate(
        name="contract-author",
        summary=(
            "Writes the contract/acceptance tests a slice is graded by. "
            "Ephemeral agents only, like every executor."
        ),
        tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
        model_tier="frontier",
        runtime_hints={"dispatch": "http", "brain": "coding"},
    ),
}


CREW_TEMPLATES: dict[str, CrewTemplate] = {
    "feature-delivery": CrewTemplate(
        name="feature-delivery",
        summary="One coding executor for an implement run.",
        agents={
            "executor": EphemeralAgent(
                role_template="backend-engineer",
                tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
            ),
        },
    ),
    "feature-review": CrewTemplate(
        name="feature-review",
        summary="One reviewer for a review run, against a sandbox from the PR ref.",
        agents={
            "reviewer": EphemeralAgent(
                role_template="sdet",
                tools=tuple(SANDBOX_TOOLS + REVIEW_TOOLS + ["issues.comment"]),
            ),
        },
    ),
    # One author, working from the contract ticket itself. A distinct
    # crew template is load-bearing twice over — the crew-request row keys
    # on `sha256(conductor|crew_template|run_id)`, so a same-template
    # second crew in one run would collide onto one row, and reviewer
    # independence rejects on shared `crew_template`, so the spec's author
    # stays positionally independent of implementer and reviewer alike.
    "contract-authoring": CrewTemplate(
        name="contract-authoring",
        summary="One contract author delivering the acceptance tests for a slice.",
        agents={
            "author": EphemeralAgent(
                role_template="contract-author",
                tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
            ),
        },
    ),
}
