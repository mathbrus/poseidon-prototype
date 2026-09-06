"""The versioned catalogs: the roles a company may hire or spawn, and the
crews that fulfil a run.

A blueprint names roles and crews; the compiler resolves them into agent
specs and per-agent Rego bundles. A role carries its tool ceiling, model
tier, spawn authority and the procedures it may run — never counts, never
secrets: how many of a role a company hires is the blueprint's business,
and credentials never enter a spec at all.

One rule the catalog encodes rather than documents: `implementer`,
`reviewer`, `contract-author` and `steward` exist **only** as ephemeral
agents. The schema does not forbid standing counts structurally — the
compiler error does.
"""

from dataclasses import dataclass

from common.tasks import DOCUMENT_CREW
from schema.tools import ARCHITECTURE_TOOLS, ISSUE_TOOLS, REVIEW_TOOLS, SANDBOX_TOOLS

# The agent host's units of work. A dispatch names one; a role declares
# which it may run, and the host refuses the rest before any model call.
PROCEDURES = ("scope", "decide", "implement", "deliver", "review")

# The one role that writes the company's documents — the compiler exempts
# it, and it alone, from the deny on the document paths — and the crew a
# bootstrap ticket or an architecture request routes to.
DOCUMENT_ROLE = "steward"


@dataclass(frozen=True)
class Role:
    name: str
    summary: str
    # Tools an agent of this role may call, before the blueprint narrows
    # them further. The compiler emits the intersection.
    tools: tuple[str, ...]
    # The procedures a dispatch may ask an agent of this role to run.
    procedures: tuple[str, ...]
    # Crews the platform may fulfil under this role's authority, when the
    # blueprint grants them: the compiled `crew.request` decision every
    # fulfilment must win is evaluated as this role.
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
            "Standing conductor: decides the company's next action on every "
            "heartbeat tick, scopes the next slice of work into tickets, and "
            "is the authority every crew is fulfilled under. Holds no coding "
            "tools — the PM cannot open a pull request or write in a sandbox."
        ),
        tools=tuple(ISSUE_TOOLS),
        procedures=("scope", "decide"),
        may_spawn=(
            "contract-authoring",
            "feature-delivery",
            "feature-review",
            "stewardship",
        ),
    ),
    "implementer": Role(
        name="implementer",
        summary=(
            "Implements a scoped task in the run's sandbox and delivers it as "
            "a pull request. Ephemeral agents only; standing counts are a "
            "compile error."
        ),
        tools=tuple(SANDBOX_TOOLS + ARCHITECTURE_TOOLS + ["git.open_pr"]),
        procedures=("implement", "deliver"),
    ),
    "reviewer": Role(
        name="reviewer",
        summary="Reads the diff, runs the tests, posts findings, gives a verdict.",
        tools=tuple(
            SANDBOX_TOOLS + REVIEW_TOOLS + ARCHITECTURE_TOOLS + ["issues.comment"]
        ),
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
        tools=tuple(SANDBOX_TOOLS + ARCHITECTURE_TOOLS + ["git.open_pr"]),
        procedures=("implement", "deliver"),
    ),
    # The company's documents have one writer. The steward writes the
    # strategy once from the vision (the bootstrap ticket), and the
    # architecture masterplan at bootstrap and whenever an architecture
    # request asks for a revision. The compiled path fences name this role
    # as the only one that may deliver into the document paths; after the
    # strategy exists, nobody may — a future standing observer would be
    # its author, and neither this role, the PM nor `decide` grows to
    # fill that gap.
    DOCUMENT_ROLE: Role(
        name=DOCUMENT_ROLE,
        summary=(
            "Writes the company's strategy once and its architecture "
            "masterplan at bootstrap and on request. Ephemeral agents only, "
            "like the implementer."
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
    # crew is load-bearing: reviewer independence rejects on a shared
    # `crew_template`, so the spec's author stays positionally independent
    # of implementer and reviewer alike.
    "contract-authoring": CrewTemplate(
        name="contract-authoring",
        summary="One contract author delivering the acceptance tests for a slice.",
        roles=("contract-author",),
    ),
    DOCUMENT_CREW: CrewTemplate(
        name=DOCUMENT_CREW,
        summary="One steward delivering a company document.",
        roles=(DOCUMENT_ROLE,),
    ),
}
