"""In-memory stand-ins for the registry accessors — for tests only.

They live here, next to the real implementations, because two packages
(gateway and sandbox) need them and a double that drifts from its interface
is worse than no double at all. Nothing in a running service may import
this module: the point of the registry is that state survives the process.

Each double mirrors its accessor's *behaviour*, not just its signatures:
`start` upserts and preserves artifacts, `record_artifacts` is a no-op for
an unknown run (an UPDATE matching zero rows), and `story` returns the same
ten-column projection the Postgres query does. A double with friendlier
failure modes than the real thing hides exactly the bugs it exists to catch.
"""

from typing import Any

from common.audit import AuditEvent
from registry.runs import RunRecord


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def story(self, run_id: str) -> list[dict[str, Any]]:
        # The same projection and order as AuditStore.story — consumers must
        # not come to depend on extra keys Postgres will never return.
        return [
            {
                "ts": e.ts,
                "run_id": e.run_id,
                "agent_id": e.agent_id,
                "role": e.role,
                "tool": e.tool,
                "args_digest": e.args_digest,
                "decision": e.decision,
                "rule": e.rule,
                "latency_ms": e.latency_ms,
                "backend_status": e.backend_status,
            }
            for e in sorted(
                (e for e in self.events if e.run_id == run_id),
                key=lambda e: e.ts,
            )
        ]


class InMemorySandboxRegistrations:
    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def register(
        self,
        run_id: str,
        url: str,
        namespace: str | None = None,
        job_name: str | None = None,
    ) -> None:
        self.rows[run_id] = {
            "url": url,
            "namespace": namespace,
            "job_name": job_name,
            "terminated": False,
        }

    def url_for(self, run_id: str) -> str | None:
        row = self.rows.get(run_id)
        return None if row is None or row["terminated"] else row["url"]

    def terminate(self, run_id: str) -> None:
        if run_id in self.rows:
            self.rows[run_id]["terminated"] = True


class InMemoryAgentInstances:
    """The gateway's read side of `agent_instances` (workload identity,
    plan step 11) plus the operator's upsert, same row shape as Postgres."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        agent_id: str,
        company: str,
        hire: str,
        role_template: str,
        model_tier: str,
        namespace: str | None = None,
        status: str = "ready",
    ) -> None:
        self.rows[agent_id] = {
            "agent_id": agent_id,
            "company": company,
            "hire": hire,
            "role_template": role_template,
            "model_tier": model_tier,
            "namespace": namespace,
            "status": status,
        }

    def get(self, agent_id: str) -> dict[str, Any] | None:
        return self.rows.get(agent_id)


class InMemoryRunParticipants:
    """Live ephemeral identities; `active` goes dark on termination — the
    behaviour the revocation-before-TTL criterion rides on."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def add(
        self,
        agent_id: str,
        run_id: str,
        parent_agent_id: str,
        crew_template: str,
        role: str,
    ) -> None:
        self.rows[agent_id] = {
            "agent_id": agent_id,
            "run_id": run_id,
            "parent_agent_id": parent_agent_id,
            "crew_template": crew_template,
            "role": role,
            "status": "active",
        }

    def terminate(self, agent_id: str) -> None:
        if agent_id in self.rows:
            self.rows[agent_id]["status"] = "terminated"

    def active(self, agent_id: str) -> dict[str, Any] | None:
        row = self.rows.get(agent_id)
        return row if row is not None and row["status"] == "active" else None


class InMemoryRuns:
    def __init__(self) -> None:
        self.rows: dict[str, RunRecord] = {}

    def start(
        self,
        run_id: str,
        task: str,
        workflow: str = "implement",
        workflow_id: str | None = None,
        company: str | None = None,
    ) -> None:
        # Upsert like the real accessor: a retried start lands on the same
        # row and keeps recorded artifacts, resetting only the run state.
        record = self.rows.get(run_id) or RunRecord(run_id=run_id)
        record.task = task
        record.status = "running"
        record.workflow = workflow
        record.workflow_id = workflow_id
        record.company = company
        record.error = None
        record.finished_at = None
        self.rows[run_id] = record

    def record_artifacts(
        self, run_id: str, pr_url: str | None = None, ticket_urls: list[str] | None = None
    ) -> None:
        record = self.rows.get(run_id)
        if record is None:  # an UPDATE matching zero rows, not a KeyError
            return
        record.pr_url = pr_url or record.pr_url
        record.ticket_urls = ticket_urls or record.ticket_urls

    def finish(
        self,
        run_id: str,
        status: str,
        pr_url: str | None = None,
        iterations: int = 0,
        error: str | None = None,
    ) -> RunRecord:
        record = self.rows.get(run_id) or RunRecord(run_id=run_id)
        record.status = status
        record.pr_url = pr_url or record.pr_url
        record.iterations = iterations
        record.error = error
        record.finished_at = "2026-01-01T00:00:00+00:00"
        self.rows[run_id] = record
        return record

    def get(self, run_id: str) -> RunRecord | None:
        return self.rows.get(run_id)

    def recent(self, limit: int = 20) -> list[RunRecord]:
        # Insertion order stands in for created_at, newest first.
        return list(self.rows.values())[-limit:][::-1]
