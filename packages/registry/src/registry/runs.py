"""The `runs` table — written by the orchestrator only.

One row per run: its task, workflow, status and artifacts. The row is the
workflow's terminal record and what the CLI prints when a run completes.

The row also says whether the run *concluded* its work item, which is a
different question from whether it succeeded: an implement run concluded
when it delivered a PR (`pr_url`) and that delivery was not refused
(`pr_settled`), a review run when its reviewer reached a verdict
(`verdict`) — a rework filed or a gate rejected is still a review that
concluded. An implement run that ended short of a delivery aborted (a
timed-out model call, a step cap, a cancel); one whose delivery was
refused spent an attempt on its ticket and gave it back. The heartbeat's
consumption rule reads that difference off the attempt aggregates below.
"""

import json
from dataclasses import dataclass, field
from typing import Any

from psycopg.errors import UniqueViolation

from common.tasks import ISSUE_TASK_RE, REVIEW_TASK_RE, issue_run_task
from common.workflows import HEARTBEAT_WORKFLOW, PR_REFUSED, RUN_WORKFLOWS
from registry.db import Registry


@dataclass(frozen=True)
class Attempts:
    """Every run ever made on one work item, folded: how many, whether one
    is still running, and whether any concluded it."""

    count: int
    live: bool
    concluded: bool


@dataclass
class RunRecord:
    run_id: str
    task: str = ""
    status: str = "running"
    occupies: bool = True
    workflow: str = "implement"
    workflow_id: str | None = None
    company: str | None = None
    pr_url: str | None = None
    pr_settled: str | None = None
    pr_settled_reason: str | None = None
    verdict: str | None = None
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
            occupies=row["occupies"],
            workflow=row["workflow"],
            workflow_id=row["workflow_id"],
            company=row["company"],
            pr_url=row["pr_url"],
            pr_settled=row["pr_settled"],
            pr_settled_reason=row["pr_settled_reason"],
            verdict=row["verdict"],
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
            "verdict": self.verdict,
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
        while a *different* run claiming the same live issue or PR task
        still dies on the one-live-run indexes."""
        try:
            self._db.execute(
                """
                INSERT INTO registry.runs
                    (run_id, task, status, workflow, workflow_id, company)
                VALUES (%s, %s, 'running', %s, %s, %s)
                ON CONFLICT (run_id) DO UPDATE SET
                    task = EXCLUDED.task,
                    status = 'running',
                    occupies = true,
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
                f"run {run_id!r} refused: another live {workflow} run already"
                f" holds {task!r} — one live run per work item"
            ) from exc

    def record_artifacts(
        self, run_id: str, pr_url: str | None = None, ticket_urls: list[str] | None = None
    ) -> None:
        """The run's artifacts as facts on its row. `pr_url` is the one
        delivery; `ticket_urls` accumulate — the ticket a run works, the
        tickets its dispatches file — each URL once, in the order written."""
        self._db.execute(
            """
            UPDATE registry.runs
               SET pr_url = COALESCE(%s, pr_url),
                   ticket_urls = ticket_urls || (
                       SELECT COALESCE(jsonb_agg(e ORDER BY ord), '[]'::jsonb)
                         FROM (SELECT DISTINCT ON (e) e, ord
                                 FROM jsonb_array_elements(%s::jsonb)
                                      WITH ORDINALITY AS t(e, ord)
                                ORDER BY e, ord) AS incoming
                        WHERE NOT ticket_urls @> jsonb_build_array(e))
             WHERE run_id = %s
            """,
            (pr_url, json.dumps(ticket_urls or []), run_id),
        )

    def record_verdict(self, run_id: str, verdict: str) -> None:
        """The review's conclusion, as a fact on its row — mirroring
        `pr_url` for implement. Written beside the journaled `review.verdict`
        audit event; this is the copy the consumption rule reads, so `Runs`
        never has to read the audit ledger."""
        self._db.execute(
            "UPDATE registry.runs SET verdict = %s WHERE run_id = %s",
            (verdict, run_id),
        )

    def finish(
        self,
        run_id: str,
        status: str,
        pr_url: str | None = None,
        iterations: int = 0,
        error: str | None = None,
    ) -> RunRecord:
        """Write the honest terminal record on the run's row and return it.
        A run refused at birth has no row and gets none: its record is
        returned, not written, since a row without its company would
        outlive the company's teardown sweep."""
        rows = self._db.query(
            """
            UPDATE registry.runs
               SET status = %s,
                   pr_url = COALESCE(%s, pr_url),
                   iterations = %s,
                   error = %s,
                   finished_at = now()
             WHERE run_id = %s
            RETURNING *
            """,
            (status, pr_url, iterations, error, run_id),
        )
        if rows:
            return RunRecord.from_row(rows[0])
        return RunRecord(
            run_id=run_id,
            status=status,
            pr_url=pr_url,
            iterations=iterations,
            error=error,
        )

    def get(self, run_id: str) -> RunRecord | None:
        rows = self._db.query("SELECT * FROM registry.runs WHERE run_id = %s", (run_id,))
        return RunRecord.from_row(rows[0]) if rows else None

    def running(self, company: str) -> list[RunRecord]:
        """The company's runs still running — what a delete cancels before
        the namespace they execute in goes."""
        rows = self._db.query(
            "SELECT * FROM registry.runs WHERE company = %s AND status = 'running'"
            " AND workflow <> %s ORDER BY created_at, run_id",
            (company, HEARTBEAT_WORKFLOW),
        )
        return [RunRecord.from_row(row) for row in rows]

    def active_count(self, company: str) -> int:
        """Runs currently occupying the company — what the heartbeat
        compares against `max_concurrent_runs`: every run the heartbeat
        may start, the conductor's own scope run included, but a run that
        gave its place back while it waits on a person (`vacate`). Only
        the heartbeat's decision rows are not runs."""
        return self._running(company, occupies=True)

    def vacated_count(self, company: str) -> int:
        """Runs still running that hold no place: parked on a person's
        merge verdict, their crew and sandbox given back."""
        return self._running(company, occupies=False)

    def _running(self, company: str, occupies: bool) -> int:
        rows = self._db.query(
            "SELECT COUNT(*) AS n FROM registry.runs"
            " WHERE company = %s AND status = 'running' AND occupies = %s"
            "   AND workflow = ANY(%s)",
            (company, occupies, list(RUN_WORKFLOWS)),
        )
        return int(rows[0]["n"])

    def vacate(self, run_id: str) -> None:
        """The run gives its place under `max_concurrent_runs` back while
        it waits on a person; it is still running."""
        self._db.execute(
            "UPDATE registry.runs SET occupies = false WHERE run_id = %s", (run_id,)
        )

    def issue_attempts(self, company: str) -> dict[int, Attempts]:
        """Every issue the company's implement runs ever took up, keyed by
        number, with the runs on it folded into one `Attempts`. Concluded
        means delivered and not refused: a `pr_url` on any of the runs,
        whatever their status — a failed teardown after delivery still
        delivered — unless the platform refused that delivery, which gives
        the ticket back as one spent attempt."""
        return self._attempts(
            company,
            "implement",
            ISSUE_TASK_RE.pattern,
            f"pr_url IS NOT NULL AND pr_settled IS DISTINCT FROM '{PR_REFUSED}'",
        )

    def review_attempts(self, company: str) -> dict[int, Attempts]:
        """Every PR the company's review runs ever took up, keyed by
        number. Concluded means a verdict was reached — a rework filed or
        a gate rejected on that verdict is still a review that concluded,
        and stays consumed."""
        return self._attempts(
            company, "review", REVIEW_TASK_RE.pattern, "verdict IS NOT NULL"
        )

    def _attempts(
        self, company: str, workflow: str, task_pattern: str, concluded: str
    ) -> dict[int, Attempts]:
        rows = self._db.query(
            f"SELECT (regexp_match(task, %s))[1]::int AS number,"
            f"       COUNT(*) AS count,"
            f"       bool_or(status = 'running') AS live,"
            f"       bool_or({concluded}) AS concluded"
            f"  FROM registry.runs"
            f" WHERE company = %s AND workflow = %s AND task ~ %s"
            f" GROUP BY 1",
            (task_pattern, company, workflow, task_pattern),
        )
        return {
            int(row["number"]): Attempts(
                count=int(row["count"]),
                live=bool(row["live"]),
                concluded=bool(row["concluded"]),
            )
            for row in rows
        }

    def delivered_pr_urls(self, company: str) -> list[str]:
        """PRs the company's implement runs have opened and finished with,
        and the world has not settled — the review side of the heartbeat's
        observation, before the consumed ones (`review_attempts`) are
        subtracted."""
        rows = self._db.query(
            "SELECT pr_url FROM registry.runs"
            " WHERE company = %s AND workflow = 'implement'"
            "   AND status = 'done' AND pr_url IS NOT NULL"
            "   AND pr_settled IS NULL"
            " ORDER BY finished_at",
            (company,),
        )
        return [row["pr_url"] for row in rows]

    def mark_pr_settled(
        self, company: str, pr_number: int, state: str, reason: str | None = None
    ) -> None:
        """What became of a delivered PR (`common.workflows.PR_SETTLED`),
        and why when it was refused: remembered on the delivering run so
        no later observation reads it again. Keyed the way every reader
        keys PRs — by number, the URL's tail."""
        self._db.execute(
            "UPDATE registry.runs SET pr_settled = %s, pr_settled_reason = %s"
            " WHERE company = %s AND workflow = 'implement' AND pr_url ~ %s",
            (state, reason, company, f"/pull/{pr_number}$"),
        )

    def refusals(self, company: str, issue_number: int) -> list[tuple[str, str]]:
        """The ticket's refused deliveries as `(pr_url, reason)`, oldest
        first — what the next attempt's brief says went wrong before."""
        rows = self._db.query(
            "SELECT pr_url, pr_settled_reason FROM registry.runs"
            " WHERE company = %s AND workflow = 'implement' AND task = %s"
            "   AND pr_settled = %s"
            " ORDER BY finished_at NULLS LAST, created_at",
            (company, issue_run_task(issue_number), PR_REFUSED),
        )
        return [(row["pr_url"], row["pr_settled_reason"] or "") for row in rows]

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

    def since(self, company: str, since: str | None) -> list[RunRecord]:
        """The company's runs created at or after a fence (every run when
        none is given), oldest first — a reader's window onto what the
        company did after it took its clock."""
        rows = self._db.query(
            "SELECT * FROM registry.runs WHERE company = %s AND workflow <> %s"
            " AND (%s::timestamptz IS NULL OR created_at >= %s::timestamptz)"
            " ORDER BY created_at, run_id",
            (company, HEARTBEAT_WORKFLOW, since, since),
        )
        return [RunRecord.from_row(row) for row in rows]

    def recent(self, company: str, limit: int = 20) -> list[RunRecord]:
        """The company's latest runs, newest first — the observation's run
        history."""
        # Heartbeat decision rows exist only so the heartbeat's LLM call can
        # bind; they are not work the company did, so they never enter the
        # observation's run history.
        rows = self._db.query(
            "SELECT * FROM registry.runs WHERE company = %s AND workflow <> %s"
            " ORDER BY created_at DESC LIMIT %s",
            (company, HEARTBEAT_WORKFLOW, limit),
        )
        return [RunRecord.from_row(row) for row in rows]
