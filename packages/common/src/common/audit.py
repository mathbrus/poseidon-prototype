"""Audit event model, and the names the platform's own events carry.

Multi-source attribution: `company`, `lifecycle`, `parent_agent_id`,
`workflow`, and an explicit `source`. One model, one registry table, one
ingestion contract — every producer writes the same shape and one `run_id`
query returns the whole story across sources. Args remain digests: audit
must never become a leak channel. The one cleartext field is the
rationale — the reason an actor stated for a decision that committed the
company to something — bounded, and never the arguments or the prompt.

Sources: the MCP gateway's decision pipeline; the egress proxy's
allows/denies (`sandbox-infra`); the LLM gateway; `process-lifecycle`, the
orchestrator's fulfill machinery (identity mint/revoke, ephemeral-pod
provisioning); the process gates; and the console, the human's
chokepoint, whose events are the only ones carrying a person's name.

An event's `tool` column names it. The gateway's rows carry a gateway
tool (`blueprint.tools`); every other row carries a name the platform
invents, and `PLATFORM_EVENTS` is the closed list of those: the writer
and every reader — the console, the acceptance suite, the reference
docs — spell each one from here. The names that are also OPA decisions
(`heartbeat.start`, `merge.resolve`, the console's acts) are defined in
`common.policy`, the naming contract the compiler emits rules from, and
listed here.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal, NamedTuple, get_args

from pydantic import BaseModel, Field

from common.policy import (
    BLUEPRINT_APPLY,
    COMPANY_DELETE,
    GATE_RESOLVE,
    HEARTBEAT_PAUSE,
    HEARTBEAT_RESUME,
    HEARTBEAT_START,
    HEARTBEAT_WAKE,
    MERGE_RESOLVE,
    RUN_CANCEL,
    RUN_START,
)
from common.workflows import (
    ATTEMPTS_GATE,
    BOOTSTRAP_GATE,
    BUDGET_GATE,
    DAILY_CAP_GATE,
    MERGE_GATE,
)

# The most a stated reason may be: long enough to be a reason, too short
# to become a transcript. Writers clip; the model refuses longer.
RATIONALE_LIMIT = 1000

Source = Literal[
    "gateway",
    "llm-gateway",
    "sandbox-infra",
    "process-gate",
    "process-lifecycle",
    "console",
]
SOURCES: tuple[str, ...] = get_args(Source)

# The heartbeat's journal, beyond the start decision.
HEARTBEAT_TICK = "heartbeat.tick"
HEARTBEAT_DECIDE = "heartbeat.decide"
HEARTBEAT_BOOTSTRAP = "heartbeat.bootstrap"
# The review's facts, beyond the merge decision.
REVIEW_READ_PR = "review.read_pr"
REVIEW_VERDICT = "review.verdict"
REVIEW_CONTRACT_EDIT = "review.contract_edit"
# The crew lifecycle: what the platform did for an ephemeral agent.
LIFECYCLE_BIND = "lifecycle.bind"
LIFECYCLE_REVOKE = "lifecycle.revoke"
LIFECYCLE_PROVISION_POD = "lifecycle.provision_pod"
LIFECYCLE_DESTROY_POD = "lifecycle.destroy_pod"
LIFECYCLE_FULFILL = "lifecycle.fulfill"
LIFECYCLE_RECORD_REQUEST = "lifecycle.record_request"
# The egress proxy's one row per attempt.
SANDBOX_EGRESS = "sandbox.egress"


def gate_event(gate: str) -> str:
    """The `process-gate` event a gate outcome is recorded under: the gate's
    name (`common.workflows`) behind one prefix, whether a human resolved
    it, policy did, it expired, or the heartbeat halted a start."""
    return f"gate.{gate}"


class PlatformEvent(NamedTuple):
    name: str
    source: Source
    meaning: str
    # The event commits the company to something, so its row owes the
    # actor's stated reason in `rationale`. False for a fact or a refusal:
    # the rule is the reason.
    rationale: bool = False


PLATFORM_EVENTS: tuple[PlatformEvent, ...] = (
    # -- the heartbeat's journal ------------------------------------------
    PlatformEvent(
        HEARTBEAT_TICK,
        "process-lifecycle",
        "every tick: started work, idle and why, refused and why, or paused by whom",
    ),
    PlatformEvent(
        HEARTBEAT_DECIDE,
        "process-lifecycle",
        "the conductor's decision on what to start next, with its reason;"
        " an invalid decision is refused by rule",
        rationale=True,
    ),
    PlatformEvent(
        HEARTBEAT_START,
        "process-lifecycle",
        "policy's answer on starting a workflow on the company's own initiative",
    ),
    PlatformEvent(
        HEARTBEAT_BOOTSTRAP,
        "process-lifecycle",
        "a bootstrap ticket filed for a document absent on the base ref",
    ),
    # -- the review's facts -----------------------------------------------
    PlatformEvent(
        REVIEW_READ_PR,
        "process-lifecycle",
        "a review found its pull request already merged or closed, and declined",
    ),
    PlatformEvent(
        REVIEW_VERDICT,
        "process-lifecycle",
        "the reviewer's verdict on the pull request, with its reason",
        rationale=True,
    ),
    PlatformEvent(
        REVIEW_CONTRACT_EDIT,
        "process-lifecycle",
        "the pull request deletes contract tests; the counts, for the human",
    ),
    PlatformEvent(
        MERGE_RESOLVE,
        "process-lifecycle",
        "policy's answer on merging: released by a human's verdict or delegated",
    ),
    # -- the crew lifecycle -----------------------------------------------
    PlatformEvent(
        LIFECYCLE_RECORD_REQUEST,
        "process-lifecycle",
        "the conductor's crew request, as policy answered it",
    ),
    PlatformEvent(
        LIFECYCLE_BIND,
        "process-lifecycle",
        "an ephemeral agent's identity bound to its run",
    ),
    PlatformEvent(
        LIFECYCLE_PROVISION_POD,
        "process-lifecycle",
        "an ephemeral agent's pod provisioned in the company namespace",
    ),
    PlatformEvent(
        LIFECYCLE_FULFILL,
        "process-lifecycle",
        "an ephemeral agent fulfilled: bound and running",
    ),
    PlatformEvent(
        LIFECYCLE_DESTROY_POD,
        "process-lifecycle",
        "an ephemeral agent's pod destroyed at the run's teardown",
    ),
    PlatformEvent(
        LIFECYCLE_REVOKE,
        "process-lifecycle",
        "an ephemeral agent's identity revoked at the run's teardown; final",
    ),
    # -- the gates --------------------------------------------------------
    PlatformEvent(
        gate_event(MERGE_GATE),
        "process-gate",
        "the merge gate's outcome: a human's verdict, policy's resolution or"
        " decline, a rework filed or exhausted, an escalation or an expiry",
    ),
    PlatformEvent(
        gate_event(BUDGET_GATE),
        "process-gate",
        "the budget park's outcome: approved, rejected, escalated or expired",
    ),
    PlatformEvent(
        gate_event(ATTEMPTS_GATE),
        "process-gate",
        "a run spent the last attempt the blueprint allows on its text",
    ),
    PlatformEvent(
        gate_event(BOOTSTRAP_GATE),
        "process-gate",
        "every attempt on a bootstrap ticket aborted: the company cannot"
        " write its own documents",
    ),
    PlatformEvent(
        gate_event(DAILY_CAP_GATE),
        "process-gate",
        "the daily spend cap reached: nothing starts until tomorrow",
    ),
    # -- the egress proxy -------------------------------------------------
    PlatformEvent(
        SANDBOX_EGRESS,
        "sandbox-infra",
        "one sandbox egress attempt, allowed by the blueprint's hosts or denied",
    ),
    # -- the console's acts, under the person's name ----------------------
    PlatformEvent(
        GATE_RESOLVE,
        "console",
        "a human's verdict on a gate, recorded before the workflow hears it",
        rationale=True,
    ),
    PlatformEvent(RUN_START, "console", "a human started a run by hand"),
    PlatformEvent(RUN_CANCEL, "console", "a human cancelled a run", rationale=True),
    PlatformEvent(HEARTBEAT_WAKE, "console", "a human nudged the loop to look now"),
    PlatformEvent(
        HEARTBEAT_PAUSE,
        "console",
        "a human froze the loop: it starts nothing until resumed",
        rationale=True,
    ),
    PlatformEvent(
        HEARTBEAT_RESUME, "console", "a human lifted the pause", rationale=True
    ),
    PlatformEvent(BLUEPRINT_APPLY, "console", "a human applied the blueprint"),
    PlatformEvent(
        COMPANY_DELETE, "console", "a human deleted the company", rationale=True
    ),
)

# The platform events that owe a rationale — the suite asserts every row
# under these names states one.
RATIONALE_EVENTS = frozenset(e.name for e in PLATFORM_EVENTS if e.rationale)


def args_digest(payload: dict[str, Any]) -> str:
    """The audit form of a call's arguments: a sha256 over their canonical
    JSON. Every producer digests the same way, so one query correlates a
    denied call with its retry — and nothing of the arguments themselves is
    ever stored."""
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class AuditEvent(BaseModel):
    ts: str = Field(default_factory=lambda: datetime.now(UTC).isoformat())
    run_id: str
    agent_id: str
    role: str
    tool: str
    args_digest: str
    decision: Literal["allow", "deny"]
    rule: str | None
    # The actor's own reason for a decision that committed the company —
    # the PM's for what to start, the reviewer's for a verdict, a human's
    # for a gate answer or a cancel, an agent's for a ticket or a pull
    # request. None on every row that decided nothing: a refusal's reason
    # is its rule.
    rationale: str | None = Field(default=None, max_length=RATIONALE_LIMIT)
    latency_ms: int
    backend_status: Literal["ok", "error"] | None
    # -- attribution -------------------------------------------------------
    # Which enforcement point produced the event. Explicit on purpose: the
    # consumer must never have to infer it from agent_id conventions.
    source: Source
    # Empty when genuinely absent (an authn reject has no company; a
    # platform caller has no parent), never as a shortcut.
    company: str = ""
    lifecycle: str = ""
    parent_agent_id: str = ""
    workflow: str = ""
