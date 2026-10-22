"""The OPA naming contract shared by the compiler, the gateways and the
orchestrator: where a company's compiled policy lives, and how a name
becomes a Rego identifier.

The compiler emits a company's bundle under `company_package`; the gateway
and the orchestrator query its decisions at `company_decision_path`. One
transform, so a company can never end up with its callers querying an
empty document — which would fail closed (every call denied) but silently.
"""

from typing import Literal, NamedTuple

PLATFORM_DECISION_PATH = "poseidon/platform/decision"

# The two bundles OPA polls, by the fixed names its config lists them
# under: the platform's own policy and the applied company's. A platform
# hosts one company, so the company slot carries no company name — the
# reconciler stores whatever company is applied under it, and revokes it
# with a tombstone at teardown.
PLATFORM_BUNDLE = "platform"
COMPANY_BUNDLE = "company"

# The decisions the platform evaluates itself against a company's bundle.
# None is a gateway tool: each is the question the orchestrator must win
# before it acts — starting a workflow on the company's own initiative,
# recording a crew request under the conductor's authority, merging a
# pull request without a human. The compiler emits the rules under these
# names and the orchestrator queries them by the same names. The audit
# rows those queries leave carry the same name in their `tool` column, so
# `common.audit` lists these among the platform's event names.
HEARTBEAT_START = "heartbeat.start"
CREW_REQUEST = "crew.request"
MERGE_RESOLVE = "merge.resolve"

# The acts a human performs through the console, decided against the
# company's bundle exactly like the tools above: whether *this* verified
# person may resolve a gate, start or cancel a run by hand, nudge the
# heartbeat, pause it or resume it, apply the blueprint, or delete the
# company. The compiler emits the rules from `company.humans`; the
# console queries them by these names.
GATE_RESOLVE = "gate.resolve"
RUN_START = "run.start"
RUN_CANCEL = "run.cancel"
HEARTBEAT_WAKE = "heartbeat.wake"
HEARTBEAT_PAUSE = "heartbeat.pause"
HEARTBEAT_RESUME = "heartbeat.resume"
BLUEPRINT_APPLY = "blueprint.apply"
COMPANY_DELETE = "company.delete"

# The rules the blueprint's `policies` section and the company's document
# paths compile to over a delivery (`git.open_pr`), and the repository
# rule every repository tool shares — spelled once: the compiler emits
# them, the audit row carries them, and the gateway reads them to say
# what a denial refused.
REPO_NOT_DECLARED = "repo_not_declared"
GIT_OPEN_PR_PROTECTED_BRANCH = "git_open_pr_protected_branch"
GIT_OPEN_PR_BRANCH_PATTERN = "git_open_pr_branch_pattern"
GIT_OPEN_PR_PATHS_MISSING = "git_open_pr_paths_missing"
GIT_OPEN_PR_DENIED_PATHS = "git_open_pr_denied_paths"
GIT_OPEN_PR_CONTRACT_TREE = "git_open_pr_contract_tree"
GIT_OPEN_PR_ARCHITECTURE_PATH = "git_open_pr_architecture_path"
GIT_OPEN_PR_STRATEGY_PATH = "git_open_pr_strategy_path"
GIT_OPEN_PR_STRATEGY_PRESENT = "git_open_pr_strategy_present"
GIT_OPEN_PR_STRATEGY_PRESENCE_MISSING = "git_open_pr_strategy_presence_missing"


def verdict_rule(gate: str, approved: bool) -> str:
    """The rule a human's verdict on a gate is allowed and recorded under:
    the verdict named in the rule itself, so the compiler that emits the
    allow, the console that records it and the workflow that reads it
    back agree on one spelling."""
    return f"gate_resolve_{rego_slug(gate)}_{'approve' if approved else 'reject'}"


def rego_slug(value: str) -> str:
    """A name as a Rego identifier segment: dots, hyphens and slashes become
    underscores."""
    return value.replace(".", "_").replace("-", "_").replace("/", "_")


def company_package(company: str) -> str:
    """The company bundle's Rego package — also its OPA bundle root."""
    return f"poseidon.authz.{rego_slug(company)}"


def company_decision_path(company: str) -> str:
    """The OPA data path of the company's `decision` document."""
    return f"{company_package(company).replace('.', '/')}/decision"


class PolicyRule(NamedTuple):
    """One rule name a decision can answer with, as the audit row's `rule`
    column carries it. A `<placeholder>` stands for a name the compiler
    fills in per company: a standing agent, crew, role, tool, workflow or
    gate."""

    name: str
    effect: Literal["allow", "deny"]
    decides: str


# Every rule name a decision can answer with: the compiler emits all but
# the two the enforcement points supply themselves (`default_deny` is the
# bundle's default, `no_policy_installed` is read when OPA serves no
# document at all). The compiler's tests hold this list and the compiled
# bundles equal; the reference docs render it.
RULES: tuple[PolicyRule, ...] = (
    PolicyRule("default_deny", "deny", "Nothing matched."),
    PolicyRule(
        "no_policy_installed",
        "deny",
        "OPA serves no policy for the company yet; every enforcement point denies.",
    ),
    # -- scopes
    PolicyRule(
        "role_<standing>_<tool>",
        "allow",
        "A standing agent's tool scope: its catalog tools narrowed by the"
        " blueprint's `tools.<role>`.",
    ),
    PolicyRule(
        "crew_<crew>_<role>_<tool>",
        "allow",
        "An ephemeral agent's tool scope, per crew and role. Sandbox tools also"
        " require the caller to be bound to the run.",
    ),
    PolicyRule("platform_<tool>", "allow", "The platform's own tools."),
    PolicyRule(
        REPO_NOT_DECLARED,
        "deny",
        "A tool call naming a repository other than `stack.repo.url`.",
    ),
    # -- crews and architecture requests
    PolicyRule(
        "crew_request_over_cap",
        "deny",
        "Starting a crew beyond the conductor's `may_spawn.max_concurrent`.",
    ),
    PolicyRule(
        "crew_request_no_budget",
        "deny",
        "Starting a crew when the run has no budget left.",
    ),
    PolicyRule(
        "architecture_request_over_cap",
        "deny",
        "A second architecture request in one run.",
    ),
    PolicyRule(
        "architecture_request_count_missing",
        "deny",
        "An architecture request without the run's request count.",
    ),
    # -- deliveries
    PolicyRule(
        GIT_OPEN_PR_PROTECTED_BRANCH,
        "deny",
        "A delivery to a branch in `policies.branch_protection.protected`.",
    ),
    PolicyRule(
        GIT_OPEN_PR_BRANCH_PATTERN,
        "deny",
        "A delivery branch that doesn't match `branch_protection.allowed_pattern`.",
    ),
    PolicyRule(
        GIT_OPEN_PR_PATHS_MISSING,
        "deny",
        "A delivery whose changed paths are missing from the input.",
    ),
    PolicyRule(
        GIT_OPEN_PR_DENIED_PATHS,
        "deny",
        "A delivery that changes a path in `policies.deny_paths`.",
    ),
    PolicyRule(
        GIT_OPEN_PR_CONTRACT_TREE,
        "deny",
        "A delivery into the contract tree by a role `contract_tests.roles` doesn't name.",
    ),
    PolicyRule(
        GIT_OPEN_PR_STRATEGY_PATH,
        "deny",
        "A delivery into the strategy's path by any role but the steward.",
    ),
    PolicyRule(
        GIT_OPEN_PR_ARCHITECTURE_PATH,
        "deny",
        "A delivery into the masterplan's path by any role but the steward.",
    ),
    PolicyRule(
        GIT_OPEN_PR_STRATEGY_PRESENT,
        "deny",
        "A delivery over a strategy that already exists on the base branch.",
    ),
    PolicyRule(
        GIT_OPEN_PR_STRATEGY_PRESENCE_MISSING,
        "deny",
        "A delivery to the strategy's path without the fact of whether it exists.",
    ),
    # -- the heartbeat
    PolicyRule(
        "heartbeat_start_<workflow>",
        "allow",
        "The heartbeat may start this workflow: it is on `may_start`.",
    ),
    PolicyRule(
        "heartbeat_start_over_concurrency",
        "deny",
        "A start at `max_concurrent_runs`.",
    ),
    PolicyRule(
        "heartbeat_start_over_daily_cap",
        "deny",
        "A start once the day's spend reached `daily_cap.usd`.",
    ),
    PolicyRule(
        "heartbeat_start_over_attempts",
        "deny",
        "A start on an item `max_attempts` runs already took up.",
    ),
    PolicyRule(
        "heartbeat_start_over_open_tickets",
        "deny",
        "A `scope` start at `max_open_tickets`.",
    ),
    PolicyRule(
        "heartbeat_start_while_paused",
        "deny",
        "A start while a person holds the company paused.",
    ),
    PolicyRule(
        "heartbeat_start_counts_missing",
        "deny",
        "A start without the live run count.",
    ),
    PolicyRule(
        "heartbeat_start_spend_missing",
        "deny",
        "A start without the day's spend.",
    ),
    PolicyRule(
        "heartbeat_start_attempts_missing",
        "deny",
        "A start without the item's attempt count.",
    ),
    PolicyRule(
        "heartbeat_start_open_tickets_missing",
        "deny",
        "A `scope` start without the open-ticket count.",
    ),
    PolicyRule(
        "heartbeat_start_paused_missing",
        "deny",
        "A start without the fact of whether the company is paused.",
    ),
    # -- people
    PolicyRule(
        "gate_resolve_<gate>_approve",
        "allow",
        "A person `company.humans` names approves a gate.",
    ),
    PolicyRule(
        "gate_resolve_<gate>_reject",
        "allow",
        "A person `company.humans` names rejects a gate.",
    ),
    PolicyRule(
        "run_start_<workflow>",
        "allow",
        "A person `company.humans` names starts a run by hand.",
    ),
    PolicyRule("run_cancel", "allow", "A person `company.humans` names cancels a run."),
    PolicyRule(
        "heartbeat_wake", "allow", "A person `company.humans` names wakes the heartbeat."
    ),
    PolicyRule(
        "heartbeat_pause", "allow", "A person `company.humans` names pauses the company."
    ),
    PolicyRule(
        "heartbeat_resume", "allow", "A person `company.humans` names resumes it."
    ),
    PolicyRule(
        "blueprint_apply", "allow", "A person `company.humans` names applies a blueprint."
    ),
    PolicyRule(
        "company_delete", "allow", "A person `company.humans` names deletes the company."
    ),
    PolicyRule(
        "human_not_recognized",
        "deny",
        "A verified person the blueprint's `company.humans` doesn't name.",
    ),
    # -- the merge gate
    PolicyRule(
        "merge_resolve_human",
        "allow",
        "A merge on a recorded approving verdict by a person `company.humans` names.",
    ),
    PolicyRule(
        "merge_resolve_no_human_verdict",
        "deny",
        "A merge signal with no verdict recorded by the console.",
    ),
    PolicyRule(
        "merge_resolve_human_rejected",
        "deny",
        "The latest recorded verdict rejects the merge.",
    ),
    PolicyRule(
        "merge_resolve_not_a_human",
        "deny",
        "The recorded verdict's author is not in `company.humans`.",
    ),
    PolicyRule(
        "merge_resolve_resolver_missing",
        "deny",
        "A merge decision that doesn't say which resolver it asks for.",
    ),
    PolicyRule(
        "merge_resolve_delegated",
        "allow",
        "A policy merge: verdict passed, tests green, reviewer independent,"
        " every path delegated, and the contract green when one is declared.",
    ),
    PolicyRule(
        "merge_resolve_verdict_not_pass",
        "deny",
        "Policy merge refused: the reviewer's verdict failed.",
    ),
    PolicyRule(
        "merge_resolve_checks_not_green",
        "deny",
        "Policy merge refused: the platform's test run is red.",
    ),
    PolicyRule(
        "merge_resolve_not_independent",
        "deny",
        "Policy merge refused: the reviewer is the implementer.",
    ),
    PolicyRule(
        "merge_resolve_paths_missing",
        "deny",
        "Policy merge refused: the changed paths are missing from the input.",
    ),
    PolicyRule(
        "merge_resolve_carved_out_path",
        "deny",
        "Policy merge refused: a path under `.github/` or `policies.deny_paths`.",
    ),
    PolicyRule(
        "merge_resolve_undelegated_path",
        "deny",
        "Policy merge refused: a path outside `delegate.paths`.",
    ),
    PolicyRule(
        "merge_resolve_contract_path",
        "deny",
        "Policy merge refused: the pull request changes the contract tree.",
    ),
    PolicyRule(
        "merge_resolve_contract_not_green",
        "deny",
        "Policy merge refused: the base branch's contract tests fail on the pull request's code.",
    ),
)
