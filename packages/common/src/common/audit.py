"""Audit event model.

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
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

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
