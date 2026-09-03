"""Audit event model.

Multi-source attribution: `company`, `lifecycle`, `parent_agent_id`,
`workflow`, and an explicit `source`. One model, one registry table, one
ingestion contract — every producer writes the same shape and one `run_id`
query returns the whole story across sources. Args remain digests: audit
must never become a leak channel.

Sources: the MCP gateway's decision pipeline; the egress proxy's
allows/denies (`sandbox-infra`); the LLM gateway; `process-lifecycle`, the
orchestrator's fulfill machinery (identity mint/revoke, ephemeral-pod
provisioning); and the process gates.
"""

import hashlib
import json
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, Field

Source = Literal[
    "gateway", "llm-gateway", "sandbox-infra", "process-gate", "process-lifecycle"
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
