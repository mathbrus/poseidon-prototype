"""The `runs` table — written by the orchestrator only.

Successor of `runs/<id>/manifest.json`. The activities that used to write
files write rows; the shape is deliberately the same so the workflow's
terminal record (and the CLI's output) does not change meaning.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from registry.db import Registry


@dataclass
class RunRecord:
    run_id: str
    task: str = ""
    status: str = "running"
    workflow: str = "implement"
    workflow_id: str | None = None
    company: str | None = None
    pr_url: str | None = None
    ticket_urls: list[str] = field(default_factory=list)
    iterations: int = 0
    error: str | None = None
    created_at: str | None = None
    finished_at: str | None = None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> "RunRecord":
        return cls(
            run_id=row["run_id"],
            task=row["task"],
            status=row["status"],
            workflow=row["workflow"],
            workflow_id=row["workflow_id"],
            company=row["company"],
            pr_url=row["pr_url"],
            ticket_urls=list(row["ticket_urls"] or []),
            iterations=row["iterations"],
            error=row["error"],
            created_at=row["created_at"].isoformat() if row["created_at"] else None,
            finished_at=row["finished_at"].isoformat() if row["finished_at"] else None,
        )

    def as_dict(self) -> dict[str, Any]:
        """The workflow's terminal result and the CLI's `status` payload."""
        return {
            "run_id": self.run_id,
            "task": self.task,
            "status": self.status,
            "workflow": self.workflow,
            "pr_url": self.pr_url,
            "ticket_urls": self.ticket_urls,
            "iterations": self.iterations,
            "error": self.error,
            "created_at": self.created_at,
            "finished_at": self.finished_at,
        }


class Runs:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def start(
        self,
        run_id: str,
        task: str,
        workflow: str = "implement",
        workflow_id: str | None = None,
        company: str | None = None,
    ) -> None:
        """Create or reset the run row. Retry-safe: an activity re-attempt
        after a worker death must land on the same row, not a second one."""
        self._db.execute(
            """
            INSERT INTO registry.runs
                (run_id, task, status, workflow, workflow_id, company)
            VALUES (%s, %s, 'running', %s, %s, %s)
            ON CONFLICT (run_id) DO UPDATE SET
                task = EXCLUDED.task,
                status = 'running',
                workflow = EXCLUDED.workflow,
                workflow_id = EXCLUDED.workflow_id,
                company = EXCLUDED.company,
                error = NULL,
                finished_at = NULL
            """,
            (run_id, task, workflow, workflow_id, company),
        )

    def record_artifacts(
        self, run_id: str, pr_url: str | None = None, ticket_urls: list[str] | None = None
    ) -> None:
        self._db.execute(
            """
            UPDATE registry.runs
               SET pr_url = COALESCE(%s, pr_url),
                   ticket_urls = COALESCE(%s::jsonb, ticket_urls)
             WHERE run_id = %s
            """,
            (pr_url, json.dumps(ticket_urls) if ticket_urls else None, run_id),
        )

    def finish(
        self,
        run_id: str,
        status: str,
        pr_url: str | None = None,
        iterations: int = 0,
        error: str | None = None,
    ) -> RunRecord:
        """Write the honest terminal record and return it. Recreates the row
        if it is missing (a fresh database mid-run) rather than failing the
        compensation path."""
        self._db.execute(
            """
            INSERT INTO registry.runs
                (run_id, status, pr_url, iterations, error, finished_at)
            VALUES (%s, %s, %s, %s, %s, now())
            ON CONFLICT (run_id) DO UPDATE SET
                status = EXCLUDED.status,
                pr_url = COALESCE(EXCLUDED.pr_url, registry.runs.pr_url),
                iterations = EXCLUDED.iterations,
                error = EXCLUDED.error,
                finished_at = EXCLUDED.finished_at
            """,
            (run_id, status, pr_url, iterations, error),
        )
        record = self.get(run_id)
        if record is None:  # just written — only a vanished database gets here
            raise RuntimeError(f"run {run_id} missing immediately after finish()")
        return record

    def get(self, run_id: str) -> RunRecord | None:
        rows = self._db.query("SELECT * FROM registry.runs WHERE run_id = %s", (run_id,))
        return RunRecord.from_row(rows[0]) if rows else None

    def recent(self, limit: int = 20) -> list[RunRecord]:
        rows = self._db.query(
            "SELECT * FROM registry.runs ORDER BY created_at DESC LIMIT %s", (limit,)
        )
        return [RunRecord.from_row(row) for row in rows]
