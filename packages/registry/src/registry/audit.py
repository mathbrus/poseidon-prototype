"""`audit_events` — the one ingestion contract for every audit source.

Its whole point is the correlated query: one `run_id`,
one ordered story, every source — gateway, sandbox-infra, llm-gateway and
process-gate all write this table through this accessor and nothing else.
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
                 decision, rule, latency_ms, backend_status,
                 source, company, lifecycle, parent_agent_id, workflow)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s,
                    %s, %s, %s, %s, %s)
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
                event.source,
                event.company,
                event.lifecycle,
                event.parent_agent_id,
                event.workflow,
            ),
        )

    def story(self, run_id: str) -> list[dict[str, Any]]:
        """Everything the platform decided for this run, in order — across
        every source, from one query."""
        return self._db.query(
            """
            SELECT ts, run_id, agent_id, role, tool, args_digest,
                   decision, rule, latency_ms, backend_status,
                   source, company, lifecycle, parent_agent_id, workflow
              FROM registry.audit_events
             WHERE run_id = %s
             ORDER BY ts, id
            """,
            (run_id,),
        )
