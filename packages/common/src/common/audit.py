"""Audit event model (contract C1).

The append-only JSONL sink this module used to carry retired at plan step 8:
events go to the registry `audit_events` table, where one `run_id` query
returns the whole story across sources (§4.10). The model stays here
because every producer and consumer shares it; schema v2 lands at step 13.
"""

from typing import Literal

from pydantic import BaseModel


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
