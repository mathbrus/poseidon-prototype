"""In-memory stand-ins for the registry accessors — for tests only.

They live here, next to the real implementations, because two packages
(gateway and sandbox) need them and a double that drifts from its interface
is worse than no double at all. Nothing in a running service may import
this module: the point of the registry is that state survives the process.
"""

from typing import Any

from common.audit import AuditEvent


class InMemoryAuditStore:
    def __init__(self) -> None:
        self.events: list[AuditEvent] = []

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def story(self, run_id: str) -> list[dict[str, Any]]:
        return [e.model_dump() for e in self.events if e.run_id == run_id]


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


class InMemoryRuns:
    def __init__(self) -> None:
        self.rows: dict[str, Any] = {}

    def start(
        self,
        run_id: str,
        task: str,
        workflow: str = "implement",
        workflow_id: str | None = None,
        company: str | None = None,
    ) -> None:
        from registry.runs import RunRecord

        self.rows[run_id] = RunRecord(
            run_id=run_id,
            task=task,
            status="running",
            workflow=workflow,
            workflow_id=workflow_id,
            company=company,
        )

    def record_artifacts(
        self, run_id: str, pr_url: str | None = None, ticket_urls: list[str] | None = None
    ) -> None:
        record = self.rows[run_id]
        record.pr_url = pr_url or record.pr_url
        record.ticket_urls = ticket_urls or record.ticket_urls

    def finish(
        self,
        run_id: str,
        status: str,
        pr_url: str | None = None,
        iterations: int = 0,
        error: str | None = None,
    ) -> Any:
        from registry.runs import RunRecord

        record = self.rows.get(run_id) or RunRecord(run_id=run_id)
        record.status = status
        record.pr_url = pr_url or record.pr_url
        record.iterations = iterations
        record.error = error
        record.finished_at = "2026-01-01T00:00:00+00:00"
        self.rows[run_id] = record
        return record

    def get(self, run_id: str) -> Any:
        return self.rows.get(run_id)
