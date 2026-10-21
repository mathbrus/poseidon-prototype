"""The OPA naming contract shared by the compiler, the gateways and the
orchestrator: where a company's compiled policy lives, and how a name
becomes a Rego identifier.

The compiler emits a company's bundle under `company_package`; the gateway
and the orchestrator query its decisions at `company_decision_path`. One
transform, so a company can never end up with its callers querying an
empty document — which would fail closed (every call denied) but silently.
"""

PLATFORM_DECISION_PATH = "poseidon/platform/decision"

# The two bundles OPA polls, by the fixed names its config lists them
# under: the platform's own policy and the applied company's. A platform
# hosts one company, so the company slot carries no company name — the
# operator stores whatever company is applied under it, and revokes it
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
