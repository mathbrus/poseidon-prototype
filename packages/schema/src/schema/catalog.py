"""Versioned role-template and crew-template catalogs (overview §4.4, §4.5).

A blueprint names templates; the compiler resolves them into agent specs and
per-member Rego bundles. Templates carry tool scopes, model tier and runtime
hints — never counts, never secrets: how many of a role a company hires is
the blueprint's business, and credentials never enter a spec at all.

Two rules the catalog encodes rather than documents:

- `backend-engineer` and `sdet` exist **only** as crew-member material in
  the MVP end state (§4.5 rule 3). The schema does not forbid standing
  counts structurally — the compiler error does, and it lands at plan step
  19 with the firing diff, because the M2→M3 bridge blueprint must compile
  until then (sequencing choice 7).
- `analyst` is a placeholder documenting the future-monitor growth path and
  is valid only at `count: 0`. That one *is* structural: it exercises no
  tools, so there is nothing for a later step to relax.
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
    # Valid only at count: 0 (a documented growth path, not a hire).
    placeholder_only: bool = False
    # Prompt/runtime hints the brain host reads; not policy.
    runtime_hints: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class CrewMember:
    role_template: str
    # The member's explicit allow-list — deliberately NOT required to be a
    # subset of the conductor's scope (§4.5 rule 5): spawn authority is not
    # a capability ceiling.
    tools: tuple[str, ...]
    # LLM-only members get no sandbox of their own (the planner contract).
    needs_sandbox: bool = True


@dataclass(frozen=True)
class CrewTemplate:
    name: str
    summary: str
    members: dict[str, CrewMember]
    # Members inherit the run's budget; the ledger keeps them as separate
    # dimensions so crew fan-out stays attributable (§4.8).
    budget_inheritance: str = "run"


ROLE_TEMPLATES: dict[str, RoleTemplate] = {
    "pm": RoleTemplate(
        name="pm",
        summary=(
            "Standing conductor: scopes goals, writes and reads tickets, "
            "requests crews. Holds no executor tools — the PM cannot open a "
            "pull request or write in a sandbox (§4.5 exit criteria)."
        ),
        tools=tuple(ISSUE_TOOLS + CREW_TOOLS),
        model_tier="standard",
        may_spawn=("feature-delivery", "feature-review"),
        runtime_hints={
            "memory": "none",  # cross-run state is tickets + registry runs
            "dispatch": "http",
        },
    ),
    "backend-engineer": RoleTemplate(
        name="backend-engineer",
        summary=(
            "Coding executor. Crew-member material in the MVP end state; "
            "standing counts are a compile error from plan step 19."
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
    "analyst": RoleTemplate(
        name="analyst",
        summary=(
            "Placeholder for the future standing monitor (§13 growth path). "
            "Valid only at count: 0 — produces no instance and exercises no "
            "tools."
        ),
        tools=(),
        model_tier="small",
        placeholder_only=True,
    ),
}


# The planner is LLM-only: read-only against the run's one sandbox, no
# sandbox of its own, no write tools (§4.4 planner contract). It produces
# exactly one artifact — the task brief the executor's dispatch carries.
_PLANNER_TOOLS = ("sandbox.read_file", "sandbox.list_dir", "issues.read")

CREW_TEMPLATES: dict[str, CrewTemplate] = {
    "feature-delivery": CrewTemplate(
        name="feature-delivery",
        summary="Planner plus one coding executor for an implement run.",
        members={
            "planner": CrewMember(
                role_template="backend-engineer",
                tools=_PLANNER_TOOLS,
                needs_sandbox=False,
            ),
            "executor": CrewMember(
                role_template="backend-engineer",
                tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
            ),
        },
    ),
    "feature-review": CrewTemplate(
        name="feature-review",
        summary="One reviewer for a review run, against a sandbox from the PR ref.",
        members={
            "reviewer": CrewMember(
                role_template="sdet",
                tools=tuple(SANDBOX_TOOLS + REVIEW_TOOLS + ["issues.comment"]),
            ),
        },
    ),
}
