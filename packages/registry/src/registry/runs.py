"""The `runs` table — written by the orchestrator only.

One row per run: its task, workflow, status and artifacts. The row is the
workflow's terminal record and what the CLI prints when a run completes.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from psycopg.errors import UniqueViolation

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
        after a worker death must land on the same row, not a second one —
        the ON CONFLICT arbiter is run_id, so a retry updates its own row
        while a *different* run claiming the same live issue task still
        dies on the one-live-issue-run index."""
        try:
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
        except UniqueViolation as exc:
            raise RuntimeError(
                f"run {run_id!r} refused: another live implement run already"
                f" holds {task!r} — one live implement run per issue"
            ) from exc

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

    def active_count(self, company: str) -> int:
        """Runs currently occupying delivery capacity — what the heartbeat
        compares against `max_concurrent_runs`. Intake is not counted: it
        files tickets, it does not occupy a crew."""
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM registry.runs"
            " WHERE company = %s AND status = 'running'"
            "   AND workflow IN ('implement', 'review')",
            (company,),
        )
        return int(rows[0]["n"])

    def started_issue_numbers(self, company: str) -> set[int]:
        """Issues any run has ever consumed, parsed from the canonical
        issue-run task (`implement issue #N`). Every status counts: a
        failed issue-run is a recorded outcome for a human to read, not an
        invitation for the heartbeat to retry it forever."""
        rows = self._db.query(
            r"SELECT DISTINCT (regexp_match(task, '^implement issue #(\d+)$'))[1]::int"
            r" AS number FROM registry.runs"
            r" WHERE company = %s AND workflow = 'implement'"
            r"   AND task ~ '^implement issue #\d+$'",
            (company,),
        )
        return {int(row["number"]) for row in rows}

    def delivered_pr_urls(self, company: str) -> list[str]:
        """PRs the company's implement runs have opened and finished with —
        the review side of the heartbeat's observation, before the
        already-reviewed set is subtracted."""
        rows = self._db.query(
            "SELECT pr_url FROM registry.runs"
            " WHERE company = %s AND workflow = 'implement'"
            "   AND status = 'done' AND pr_url IS NOT NULL"
            " ORDER BY finished_at",
            (company,),
        )
        return [row["pr_url"] for row in rows]

    def implement_run_for_pr(self, company: str, pr_number: int) -> RunRecord | None:
        """The implement run that delivered a pull request — the PR's run
        lineage, keyed the way the heartbeat observation keys PRs: the number
        is the URL's tail. The reviewer-independence check reads this; no
        row means the PR is not the company's own delivered work, which
        fails the check closed."""
        rows = self._db.query(
            "SELECT * FROM registry.runs"
            " WHERE company = %s AND workflow = 'implement'"
            "   AND pr_url ~ %s"
            " ORDER BY finished_at DESC NULLS LAST LIMIT 1",
            (company, f"/{int(pr_number)}$"),
        )
        return RunRecord.from_row(rows[0]) if rows else None

    def reviewed_pr_numbers(self, company: str) -> set[int]:
        """PRs any review run has ever consumed, parsed from the canonical
        review-run task (`review pull request #N`) — same rule as issues:
        every status counts, a failed review is a recorded outcome."""
        rows = self._db.query(
            r"SELECT DISTINCT"
            r" (regexp_match(task, '^review pull request #(\d+)$'))[1]::int"
            r" AS number FROM registry.runs"
            r" WHERE company = %s AND workflow = 'review'"
            r"   AND task ~ '^review pull request #\d+$'",
            (company,),
        )
        return {int(row["number"]) for row in rows}

    def recent(self, limit: int = 20) -> list[RunRecord]:
        # Heartbeat decision rows exist only so the heartbeat's LLM call can
        # bind; they are not work the company did, so they never enter the
        # observation's run history. The family name is the orchestrator's
        # (`orchestrator.activities.heartbeat.FAMILY`), spelled here because
        # this package cannot import it.
        rows = self._db.query(
            "SELECT * FROM registry.runs WHERE workflow <> 'heartbeat'"
            " ORDER BY created_at DESC LIMIT %s",
            (limit,),
        )
        return [RunRecord.from_row(row) for row in rows]
