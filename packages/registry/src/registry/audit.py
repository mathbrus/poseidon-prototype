"""`audit_events` — written by the gateway and the egress proxy.

The sink the JSONL files retire into (sequencing choice 1). Its whole point
is the E4 query: one `run_id`, one ordered story, every source. Schema stays
v1 here; plan step 13 adds `company`, `lifecycle`, `parent_agent_id`,
`workflow` and an explicit `source`.
"""

from typing import Any

from common.audit import AuditEvent
from registry.db import Registry


class AuditStore:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def append(self, event: AuditEvent) -> None:
        self._db.execute(
            """
            INSERT INTO registry.audit_events
                (ts, run_id, agent_id, role, tool, args_digest,
                 decision, rule, latency_ms, backend_status)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                event.ts,
                event.run_id,
                event.agent_id,
                event.role,
                event.tool,
                event.args_digest,
                event.decision,
                event.rule,
                event.latency_ms,
                event.backend_status,
            ),
        )

    def story(self, run_id: str) -> list[dict[str, Any]]:
        """Everything the platform decided for this run, in order — across
        every source, from one query (§4.10)."""
        return self._db.query(
            """
            SELECT ts, run_id, agent_id, role, tool, args_digest,
                   decision, rule, latency_ms, backend_status
              FROM registry.audit_events
             WHERE run_id = %s
             ORDER BY ts, id
            """,
            (run_id,),
        )
