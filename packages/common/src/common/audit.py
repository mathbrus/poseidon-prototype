"""Audit event model — schema v2 (§5.2, plan step 13).

The v1 fields plus the multi-source attribution: `company`, `lifecycle`,
`parent_agent_id`, `workflow`, and an explicit `source`. One model, one
registry table, one ingestion contract — every producer writes the same
shape and one `run_id` query returns the whole story across sources
(§4.10). Args remain digests: audit must never become a leak channel.

Sources: the MCP gateway emits from step 12's pipeline; the egress proxy's
allows/denies ride the same contract (`sandbox-infra`); the LLM gateway
joins at plan step 14 and process gates at steps 18–20.
"""

from typing import Literal

from pydantic import BaseModel

Source = Literal["gateway", "llm-gateway", "sandbox-infra", "process-gate"]


class AuditEvent(BaseModel):
    ts: str
    run_id: str
    agent_id: str
    role: str
    tool: str
    args_digest: str
    decision: Literal["allow", "deny"]
    rule: str | None
    latency_ms: int
    backend_status: Literal["ok", "error"] | None
    # -- v2 (§5.2) ---------------------------------------------------------
    # Which enforcement point produced the event. Explicit on purpose: the
    # consumer must never have to infer it from agent_id conventions.
    source: Source
    # Empty when genuinely absent (an authn reject has no company; a
    # platform caller has no parent), never as a shortcut.
    company: str = ""
    lifecycle: str = ""
    parent_agent_id: str = ""
    workflow: str = ""
