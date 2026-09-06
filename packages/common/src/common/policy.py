"""The OPA naming contract shared by the compiler, the gateways and the
orchestrator: where a company's compiled policy lives, and how a name
becomes a Rego identifier.

The compiler emits a company's bundle under `company_package`; the gateway
and the orchestrator query its decisions at `company_decision_path`. One
transform, so a company can never end up with its callers querying an
empty document — which would fail closed (every call denied) but silently.
"""

PLATFORM_DECISION_PATH = "poseidon/platform/decision"

# The decisions the platform evaluates itself against a company's bundle.
# None is a gateway tool: each is the question the orchestrator must win
# before it acts — starting a workflow on the company's own initiative,
# recording a crew request under the conductor's authority, merging a
# pull request without a human. The compiler emits the rules under these
# names and the orchestrator queries them by the same names.
HEARTBEAT_START = "heartbeat.start"
CREW_REQUEST = "crew.request"
MERGE_RESOLVE = "merge.resolve"
# The heartbeat's journal entry per tick — the audit tool name a reader of
# the loop (the console, the acceptance suite) filters on.
HEARTBEAT_TICK = "heartbeat.tick"

# The acts a human performs through the console, decided against the
# company's bundle exactly like the tools above: whether *this* verified
# person may resolve a gate, start or cancel a run by hand, nudge the
# heartbeat, or apply the blueprint. The compiler emits the rules from
# `company.humans`; the console queries them by these names.
GATE_RESOLVE = "gate.resolve"
RUN_START = "run.start"
RUN_CANCEL = "run.cancel"
HEARTBEAT_WAKE = "heartbeat.wake"
BLUEPRINT_APPLY = "blueprint.apply"


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
