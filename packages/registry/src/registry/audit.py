"""`audit_events` — the one ingestion contract for every audit source.

Its whole point is the correlated query: one `run_id`,
one ordered story, every source — gateway, sandbox-infra, llm-gateway,
process-gate, process-lifecycle and console all write this table through
this accessor and nothing else.

Two reads feed policy rather than a reader: `count_allowed` (a per-run
tool cap) and `latest_human_verdict` (the console-recorded verdict the
merge gate is released on). Both read facts the platform wrote under its
own identity, which is what lets OPA decide on them.
"""

from typing import Any

from common.audit import AuditEvent
from common.policy import GATE_RESOLVE, verdict_rule
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

    def count_allowed(self, run_id: str, tool: str) -> int:
        """How many calls of `tool` this run made that were allowed and
        answered — the fact a per-run cap on a tool reads."""
        rows = self._db.query(
            """
            SELECT count(*) AS n FROM registry.audit_events
             WHERE run_id = %s AND tool = %s
               AND decision = 'allow' AND backend_status = 'ok'
            """,
            (run_id, tool),
        )
        return int(rows[0]["n"]) if rows else 0

    def latest_human_verdict(self, run_id: str, gate: str) -> dict[str, Any] | None:
        """The console's most recent verdict on this run's gate —
        `{principal, approved, ts}` — or None when no human ever answered
        through the console. Latest wins: an approval followed by a
        rejection reads as rejected."""
        rows = self._db.query(
            """
            SELECT ts, agent_id, rule FROM registry.audit_events
             WHERE run_id = %s AND source = 'console' AND tool = %s
               AND decision = 'allow' AND rule IN (%s, %s)
             ORDER BY ts DESC, id DESC
             LIMIT 1
            """,
            (run_id, GATE_RESOLVE, verdict_rule(gate, True), verdict_rule(gate, False)),
        )
        if not rows:
            return None
        row = rows[0]
        return {
            "principal": row["agent_id"],
            "approved": row["rule"] == verdict_rule(gate, True),
            "ts": row["ts"],
        }

    def story(self, run_id: str, since: str | None = None) -> list[dict[str, Any]]:
        """Everything the platform decided for this run, in order — across
        every source, from one query; at or after `since` when given (a
        registry timestamp), so a reader of a standing run's long story
        asks only for its own window."""
        return self._db.query(
            """
            SELECT ts, run_id, agent_id, role, tool, args_digest,
                   decision, rule, latency_ms, backend_status,
                   source, company, lifecycle, parent_agent_id, workflow
              FROM registry.audit_events
             WHERE run_id = %s
               AND (%s::timestamptz IS NULL OR ts >= %s::timestamptz)
             ORDER BY ts, id
            """,
            (run_id, since, since),
        )
