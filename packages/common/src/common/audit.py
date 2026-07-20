"""Audit event model and append-only JSONL sink (contract C1)."""

from pathlib import Path
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


def append(event: AuditEvent, path: Path | str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as f:
        f.write(event.model_dump_json() + "\n")
