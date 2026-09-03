"""The versioned catalogs: the roles a company may hire or spawn, and the
crews that fulfil a run.

A blueprint names roles and crews; the compiler resolves them into agent
specs and per-agent Rego bundles. A role carries its tool ceiling, model
tier, spawn authority and the procedures it may run — never counts, never
secrets: how many of a role a company hires is the blueprint's business,
and credentials never enter a spec at all.

One rule the catalog encodes rather than documents: `implementer`,
`reviewer` and `contract-author` exist **only** as ephemeral agents. The
schema does not forbid standing counts structurally — the compiler error
does.
"""

from dataclasses import dataclass

from schema.tools import CREW_TOOLS, ISSUE_TOOLS, REVIEW_TOOLS, SANDBOX_TOOLS

CATALOG_VERSION = "v1"


# The agent host's units of work. A dispatch names one; a role declares
# which it may run, and the host refuses the rest before any model call.
PROCEDURES = ("scope", "decide", "implement", "deliver", "review")


@dataclass(frozen=True)
class Role:
    name: str
    summary: str
    # Tools an agent of this role may call, before the blueprint narrows
    # them further. The compiler emits the intersection.
    tools: tuple[str, ...]
    # The procedures a dispatch may ask an agent of this role to run.
    procedures: tuple[str, ...]
    # Crews this role may request, when the blueprint grants them.
    may_spawn: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrewTemplate:
    """The roles fulfilled together for one run. Each becomes one
    ephemeral agent — one principal, one pod, one identity — under the
    conductor's authority."""

    name: str
    summary: str
    roles: tuple[str, ...]


ROLES: dict[str, Role] = {
    "pm": Role(
        name="pm",
        summary=(
            "Standing conductor: scopes goals, writes, reads and comments "
            "tickets, requests crews. Holds no coding tools — the PM cannot "
            "open a pull request or write in a sandbox."
        ),
        tools=tuple(ISSUE_TOOLS + CREW_TOOLS),
        procedures=("scope", "decide"),
        may_spawn=("contract-authoring", "feature-delivery", "feature-review"),
    ),
    "implementer": Role(
        name="implementer",
        summary=(
            "Implements a scoped task in the run's sandbox and delivers it as "
            "a pull request. Ephemeral agents only; standing counts are a "
            "compile error."
        ),
        tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
        procedures=("implement", "deliver"),
    ),
    "reviewer": Role(
        name="reviewer",
        summary="Reads the diff, runs the tests, posts findings, gives a verdict.",
        tools=tuple(SANDBOX_TOOLS + REVIEW_TOOLS + ["issues.comment"]),
        procedures=("review",),
    ),
    # Deliver-capable on purpose, unlike `reviewer`: the contract tree is
    # delivered through the same governed `git.open_pr` as any change, and
    # `policies.contract_tests` is what fences who may touch it. A distinct
    # role (not `implementer`) is what lets that fence name the spec's
    # author without naming the implementer.
    "contract-author": Role(
        name="contract-author",
        summary=(
            "Writes the contract/acceptance tests a slice is graded by. "
            "Ephemeral agents only, like the implementer."
        ),
        tools=tuple(SANDBOX_TOOLS + ["git.open_pr"]),
        procedures=("implement", "deliver"),
    ),
}


CREW_TEMPLATES: dict[str, CrewTemplate] = {
    "feature-delivery": CrewTemplate(
        name="feature-delivery",
        summary="One implementer for an implement run.",
        roles=("implementer",),
    ),
    "feature-review": CrewTemplate(
        name="feature-review",
        summary="One reviewer for a review run, against a sandbox from the PR ref.",
        roles=("reviewer",),
    ),
    # One author, working from the contract ticket itself. A distinct
    # crew is load-bearing twice over — the crew-request row keys on
    # `sha256(conductor|crew_template|run_id)`, so a same-template second
    # crew in one run would collide onto one row, and reviewer independence
    # rejects on shared `crew_template`, so the spec's author stays
    # positionally independent of implementer and reviewer alike.
    "contract-authoring": CrewTemplate(
        name="contract-authoring",
        summary="One contract author delivering the acceptance tests for a slice.",
        roles=("contract-author",),
    ),
}
