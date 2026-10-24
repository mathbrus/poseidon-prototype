"""In-memory stand-ins for the registry accessors — for tests only.

They live here, next to the real implementations, because four packages'
test suites (gateway, llm-gateway, orchestrator and sandbox) need them and
a double that drifts from its interface is worse than no double at all.
Nothing in a running service may import this module: the point of the
registry is that state survives the process.

Each double mirrors its accessor's *behaviour*, not just its signatures:
`start` upserts and preserves artifacts, `record_artifacts` is a no-op for
an unknown run (an UPDATE matching zero rows), and `story` returns the same
column projection the Postgres query does. A double with friendlier
failure modes than the real thing hides exactly the bugs it exists to catch.
"""

import re
import time
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any

from common.audit import AuditEvent
from common.policy import GATE_RESOLVE, verdict_rule
from common.tasks import ISSUE_TASK_RE, REVIEW_TASK_RE, issue_run_task
from common.workflows import (
    HEARTBEAT_WORKFLOW,
    PR_REFUSED,
    RUN_WORKFLOWS,
    heartbeat_run_id,
    workflow_id,
)
from registry.participants import ParticipantRevoked
from registry.runs import Attempts, RunRecord


class InMemoryAuditStore:
    """`companies` is what `append_while_applied` checks the event's
    company against, as the real store checks the companies table."""

    def __init__(self, companies: "InMemoryCompanies | None" = None) -> None:
        self.events: list[AuditEvent] = []
        self._companies = companies

    def append(self, event: AuditEvent) -> None:
        self.events.append(event)

    def append_while_applied(self, event: AuditEvent) -> bool:
        if self._companies is None:
            raise RuntimeError("this audit double was built without its companies")
        if self._companies.get(event.company) is None:
            return False
        self.events.append(event)
        return True

    def count_allowed(self, run_id: str, tool: str) -> int:
        return sum(
            1
            for e in self.events
            if e.run_id == run_id
            and e.tool == tool
            and e.decision == "allow"
            and e.backend_status == "ok"
        )

    def latest_human_verdict(self, run_id: str, gate: str) -> dict[str, Any] | None:
        rules = {verdict_rule(gate, True), verdict_rule(gate, False)}
        verdicts = [
            e
            for e in self.events
            if e.run_id == run_id
            and e.source == "console"
            and e.tool == GATE_RESOLVE
            and e.decision == "allow"
            and e.rule in rules
        ]
        if not verdicts:
            return None
        last = max(enumerate(verdicts), key=lambda item: (item[1].ts, item[0]))[1]
        return {
            "principal": last.agent_id,
            "approved": last.rule == verdict_rule(gate, True),
            "rationale": last.rationale or "",
            "ts": last.ts,
        }

    def last_at(self, run_id: str, tool: str, rule: str) -> Any:
        matching = [
            e.ts
            for e in self.events
            if e.run_id == run_id and e.tool == tool and e.rule == rule
        ]
        return max(matching) if matching else None

    def after(
        self, company: str, after: int, run_id: str | None = None, limit: int = 500
    ) -> list[dict[str, Any]]:
        # The fake's ids are positions in the append order, from 1.
        rows = [
            {"id": index, **self._row(e)}
            for index, e in enumerate(self.events, start=1)
            if e.company == company
            and index > after
            and (run_id is None or e.run_id == run_id)
        ]
        return rows[:limit]

    @staticmethod
    def _row(e: AuditEvent) -> dict[str, Any]:
        # The same projection and order as AuditStore.story, less the id the
        # callers prepend — consumers must not come to depend on extra keys
        # Postgres will never return.
        return {
            "ts": e.ts,
            "run_id": e.run_id,
            "agent_id": e.agent_id,
            "role": e.role,
            "tool": e.tool,
            "args_digest": e.args_digest,
            "decision": e.decision,
            "rule": e.rule,
            "rationale": e.rationale,
            "latency_ms": e.latency_ms,
            "backend_status": e.backend_status,
            "source": e.source,
            "company": e.company,
            "lifecycle": e.lifecycle,
            "parent_agent_id": e.parent_agent_id,
            "workflow": e.workflow,
        }

    def story(
        self, run_id: str, since: str | None = None, decisions_only: bool = False
    ) -> list[dict[str, Any]]:
        # The fake's ids are positions in the append order, from 1.
        return [
            {"id": index, **self._row(e)}
            for index, e in sorted(
                (
                    (index, e)
                    for index, e in enumerate(self.events, start=1)
                    if e.run_id == run_id
                    and (since is None or e.ts >= since)
                    and (not decisions_only or e.rationale is not None)
                ),
                key=lambda item: (item[1].ts, item[0]),
            )
        ]


class InMemoryBudgetLedger:
    """The LLM gateway's meter: recorded entries and the
    per-run SUM the cap's refusal compares against."""

    def __init__(self) -> None:
        self.entries: list[dict[str, Any]] = []

    def record(
        self,
        *,
        company: str,
        run_id: str,
        agent_id: str,
        parent_agent_id: str,
        role: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        cached_tokens: int,
        reasoning_tokens: int,
        usd: float,
    ) -> None:
        self.entries.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "company": company,
                "run_id": run_id,
                "agent_id": agent_id,
                "parent_agent_id": parent_agent_id,
                "role": role,
                "model": model,
                "prompt_tokens": prompt_tokens,
                "completion_tokens": completion_tokens,
                "cached_tokens": cached_tokens,
                "reasoning_tokens": reasoning_tokens,
                "usd": usd,
            }
        )

    def spent_for_run(self, run_id: str) -> float:
        return float(sum(e["usd"] for e in self.entries if e["run_id"] == run_id))

    def spent_today(self, company: str) -> float:
        # The fake's day is the fake's lifetime: tests build today's ledger.
        return float(sum(e["usd"] for e in self.entries if e["company"] == company))

    def entries_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return [dict(e) for e in self.entries if e["run_id"] == run_id]

    def spent_since(self, company: str, since: str | None) -> dict[str, Any]:
        mine = [
            e
            for e in self.entries
            if e["company"] == company and (since is None or e["ts"] >= since)
        ]
        return {
            "usd": float(sum(e["usd"] for e in mine)),
            "prompt_tokens": int(sum(e["prompt_tokens"] for e in mine)),
            "completion_tokens": int(sum(e["completion_tokens"] for e in mine)),
        }


class InMemoryFiledTickets:
    def __init__(self) -> None:
        self.rows: dict[tuple[str, int], tuple[str, float]] = {}

    def record(self, repo: str, number: int, run_id: str) -> None:
        self.rows.setdefault((repo, number), (run_id, time.time()))

    def filed_since(self, repo: str, seconds: int) -> list[int]:
        horizon = time.time() - seconds
        return sorted(
            number
            for (r, number), (_run_id, filed_at) in self.rows.items()
            if r == repo and filed_at > horizon
        )

    def filed(self, repo: str) -> list[dict[str, Any]]:
        return [
            {
                "number": number,
                "run_id": run_id,
                "filed_at": datetime.fromtimestamp(filed_at, UTC).isoformat(),
            }
            for (r, number), (run_id, filed_at) in sorted(self.rows.items())
            if r == repo
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
            "run_id": run_id,
            "url": url,
            "namespace": namespace,
            "job_name": job_name,
            "terminated": False,
        }

    def url_for(self, run_id: str) -> str | None:
        row = self.active(run_id)
        return row["url"] if row else None

    def active(self, run_id: str) -> dict[str, Any] | None:
        row = self.rows.get(run_id)
        if row is None or row["terminated"]:
            return None
        return {k: row[k] for k in ("run_id", "url", "namespace", "job_name")}

    def terminate(self, run_id: str) -> None:
        if run_id in self.rows:
            self.rows[run_id]["terminated"] = True


class InMemoryCompanies:
    """The reconciler's compiled spec, the read side the worker and egress
    proxy consume the infra IR through, and the console's pause."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}
        self.pauses: dict[str, dict[str, Any]] = {}

    def upsert(self, name: str, spec: dict[str, Any]) -> None:
        self.rows[name] = dict(spec)

    def pause(self, name: str, by: str) -> dict[str, Any]:
        self.pauses[name] = {"by": by, "at": datetime.now(UTC).isoformat()}
        return self.pauses[name]

    def resume(self, name: str) -> None:
        self.pauses.pop(name, None)

    def paused(self, name: str) -> dict[str, Any] | None:
        return self.pauses.get(name) if name in self.rows else None

    def get(self, name: str) -> dict[str, Any] | None:
        return self.rows.get(name)

    def applied(self) -> str | None:
        if len(self.rows) > 1:
            raise RuntimeError(f"more than one company is applied: {sorted(self.rows)}")
        return next(iter(self.rows), None)

    def by_namespace(self, namespace: str) -> dict[str, Any] | None:
        for spec in self.rows.values():
            if spec.get("namespace") == namespace:
                return spec
        return None


class InMemoryAgentInstances:
    """The gateway's read side of `agent_instances` (workload identity)
    plus the reconciler's upsert, same row shape as Postgres."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def upsert(
        self,
        agent_id: str,
        company: str,
        standing_name: str,
        role: str,
        namespace: str | None = None,
        status: str = "converged",
    ) -> None:
        self.rows[agent_id] = {
            "agent_id": agent_id,
            "company": company,
            "standing_name": standing_name,
            "role": role,
            "namespace": namespace,
            "status": status,
        }

    def for_company(self, company: str) -> list[dict[str, Any]]:
        return sorted(
            (dict(r) for r in self.rows.values() if r["company"] == company),
            key=lambda r: r["standing_name"],
        )

    def get(self, agent_id: str) -> dict[str, Any] | None:
        return self.rows.get(agent_id)

    def delete(self, agent_id: str) -> None:
        self.rows.pop(agent_id, None)


class InMemoryRunParticipants:
    """Live ephemeral identities; `active` goes dark on termination — the
    behaviour immediate revocation rides on."""

    def __init__(self) -> None:
        self.rows: dict[str, dict[str, Any]] = {}

    def add(
        self,
        agent_id: str,
        run_id: str,
        parent_agent_id: str,
        crew_template: str,
        role: str,
        company: str = "",
        namespace: str = "",
        service_account: str | None = None,
    ) -> None:
        # Upsert like the real accessor — including its finality rule:
        # a retry converges on a live row, but a revoked one is never
        # revived.
        existing = self.rows.get(agent_id)
        if existing is not None and existing["status"] != "active":
            raise ParticipantRevoked(
                f"ephemeral agent {agent_id} was revoked; it is not revivable"
            )
        self.rows[agent_id] = {
            "agent_id": agent_id,
            "run_id": run_id,
            "parent_agent_id": parent_agent_id,
            "crew_template": crew_template,
            "role": role,
            "company": company,
            "namespace": namespace,
            "service_account": service_account,
            "status": "active",
        }

    def terminate(self, agent_id: str) -> None:
        row = self.rows.get(agent_id)
        if row is not None and row["status"] == "active":
            row["status"] = "terminated"

    def terminate_for_run(self, run_id: str) -> list[str]:
        swept = [
            agent_id
            for agent_id, row in self.rows.items()
            if row["run_id"] == run_id and row["status"] == "active"
        ]
        for agent_id in swept:
            self.rows[agent_id]["status"] = "terminated"
        return swept

    def for_run(self, run_id: str) -> list[dict[str, Any]]:
        # Terminated rows included, like the real accessor: the lineage
        # outlives the identities.
        return [
            {
                key: row[key]
                for key in (
                    "agent_id",
                    "run_id",
                    "crew_template",
                    "role",
                    "status",
                )
            }
            for row in sorted(self.rows.values(), key=lambda r: r["agent_id"])
            if row["run_id"] == run_id
        ]

    def active_spawn_count(self, parent_agent_id: str) -> int:
        return len(
            {
                (row["run_id"], row["crew_template"])
                for row in self.rows.values()
                if row["parent_agent_id"] == parent_agent_id and row["status"] == "active"
            }
        )

    def active(self, agent_id: str) -> dict[str, Any] | None:
        row = self.rows.get(agent_id)
        return row if row is not None and row["status"] == "active" else None

    def by_service_account(self, service_account: str) -> dict[str, Any] | None:
        for row in self.rows.values():
            if (
                row.get("service_account") == service_account
                and row["status"] == "active"
            ):
                return row
        return None


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
        # And like the real accessor's one-live-run indexes: a *different*
        # run claiming the same live issue or PR task is refused.
        keyed = (workflow == "implement" and ISSUE_TASK_RE.match(task)) or (
            workflow == "review" and REVIEW_TASK_RE.match(task)
        )
        if keyed:
            for other in self.rows.values():
                if (
                    other.run_id != run_id
                    and other.company == company
                    and other.workflow == workflow
                    and other.status == "running"
                    and other.task == task
                ):
                    raise RuntimeError(
                        f"run {run_id!r} refused: another live {workflow} run"
                        f" already holds {task!r} — one live run per work item"
                    )
        record = self.rows.get(run_id) or RunRecord(run_id=run_id)
        record.task = task
        record.status = "running"
        record.occupies = True
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
        for url in ticket_urls or []:
            if url not in record.ticket_urls:
                record.ticket_urls.append(url)

    def record_verdict(self, run_id: str, verdict: str) -> None:
        record = self.rows.get(run_id)
        if record is not None:
            record.verdict = verdict

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

    def running(self, company: str) -> list[RunRecord]:
        return [
            r
            for r in self.rows.values()
            if r.company == company
            and r.status == "running"
            and r.workflow != HEARTBEAT_WORKFLOW
        ]

    def active_count(self, company: str) -> int:
        return self._running(company, occupies=True)

    def vacated_count(self, company: str) -> int:
        return self._running(company, occupies=False)

    def _running(self, company: str, occupies: bool) -> int:
        return sum(
            1
            for r in self.rows.values()
            if r.company == company
            and r.status == "running"
            and r.occupies is occupies
            and r.workflow in RUN_WORKFLOWS
        )

    def vacate(self, run_id: str) -> None:
        record = self.rows.get(run_id)
        if record is not None:
            record.occupies = False

    def issue_attempts(self, company: str) -> dict[int, Attempts]:
        return self._attempts(
            company,
            "implement",
            ISSUE_TASK_RE,
            lambda r: r.pr_url is not None and r.pr_settled != PR_REFUSED,
        )

    def review_attempts(self, company: str) -> dict[int, Attempts]:
        return self._attempts(
            company, "review", REVIEW_TASK_RE, lambda r: r.verdict is not None
        )

    def _attempts(
        self,
        company: str,
        workflow: str,
        task_re: re.Pattern[str],
        concluded: Callable[[RunRecord], bool],
    ) -> dict[int, Attempts]:
        grouped: dict[int, list[RunRecord]] = {}
        for record in self.rows.values():
            if record.company != company or record.workflow != workflow:
                continue
            match = task_re.match(record.task)
            if match:
                grouped.setdefault(int(match.group(1)), []).append(record)
        return {
            number: Attempts(
                count=len(records),
                live=any(r.status == "running" for r in records),
                concluded=any(concluded(r) for r in records),
            )
            for number, records in grouped.items()
        }

    def delivered_pr_urls(self, company: str) -> list[str]:
        return [
            r.pr_url
            for r in self.rows.values()
            if r.company == company
            and r.workflow == "implement"
            and r.status == "done"
            and r.pr_url
            and r.pr_settled is None
        ]

    def mark_pr_settled(
        self, company: str, pr_number: int, state: str, reason: str | None = None
    ) -> None:
        for r in self.rows.values():
            if (
                r.company == company
                and r.workflow == "implement"
                and (r.pr_url or "").endswith(f"/pull/{pr_number}")
            ):
                r.pr_settled = state
                r.pr_settled_reason = reason

    def refusals(self, company: str, issue_number: int) -> list[tuple[str, str]]:
        # Insertion order stands in for finished_at, oldest first.
        return [
            (r.pr_url or "", r.pr_settled_reason or "")
            for r in self.rows.values()
            if r.company == company
            and r.workflow == "implement"
            and r.task == issue_run_task(issue_number)
            and r.pr_settled == PR_REFUSED
        ]

    def implement_run_for_pr(self, company: str, pr_number: int) -> RunRecord | None:
        candidates = [
            r
            for r in self.rows.values()
            if r.company == company
            and r.workflow == "implement"
            and r.pr_url
            and re.search(rf"/{int(pr_number)}$", r.pr_url)
        ]
        candidates.sort(key=lambda r: r.finished_at or "")
        return candidates[-1] if candidates else None

    def since(self, company: str, since: str | None) -> list[RunRecord]:
        return [
            r
            for r in self.rows.values()
            if r.company == company
            and r.workflow != HEARTBEAT_WORKFLOW
            and (since is None or (r.created_at or "") >= since)
        ]

    def recent(self, company: str, limit: int = 20) -> list[RunRecord]:
        # Insertion order stands in for created_at, newest first; heartbeat
        # decision rows are not work the company did and never enter the
        # observation's run history (same workflow literal as `runs.py`).
        work = [
            r
            for r in self.rows.values()
            if r.company == company and r.workflow != HEARTBEAT_WORKFLOW
        ]
        return work[-limit:][::-1]


class InMemoryCompanySweep:
    """The teardown sweep over the doubles it is given: the same rows go,
    by company or by run id, as `CompanySweep` deletes in Postgres. A test
    passes the doubles its world holds; the others are not there to
    sweep."""

    def __init__(
        self,
        *,
        companies: InMemoryCompanies | None = None,
        instances: InMemoryAgentInstances | None = None,
        runs: InMemoryRuns | None = None,
        participants: InMemoryRunParticipants | None = None,
        sandboxes: InMemorySandboxRegistrations | None = None,
        tickets: InMemoryFiledTickets | None = None,
        ledger: InMemoryBudgetLedger | None = None,
        audit: InMemoryAuditStore | None = None,
    ) -> None:
        self._companies = companies
        self._instances = instances
        self._runs = runs
        self._participants = participants
        self._sandboxes = sandboxes
        self._tickets = tickets
        self._ledger = ledger
        self._audit = audit

    def sweep(self, company: str) -> None:
        heartbeat = workflow_id(HEARTBEAT_WORKFLOW, company)
        spec = self._companies.get(company) if self._companies else None
        repo = ((spec or {}).get("repo") or {}).get("url")
        run_ids = {heartbeat_run_id(company)}
        if self._runs:
            run_ids |= {
                r.run_id for r in self._runs.rows.values() if r.company == company
            }
        if self._audit:
            self._audit.events = [
                e
                for e in self._audit.events
                if e.company != company
                and e.run_id not in run_ids
                and e.workflow != heartbeat
            ]
        if self._ledger:
            self._ledger.entries = [
                e
                for e in self._ledger.entries
                if e["company"] != company and e["run_id"] not in run_ids
            ]
        if self._sandboxes:
            for run_id in run_ids:
                self._sandboxes.rows.pop(run_id, None)
        if self._participants:
            self._participants.rows = {
                agent_id: row
                for agent_id, row in self._participants.rows.items()
                if row["company"] != company and row["run_id"] not in run_ids
            }
        if self._tickets:
            self._tickets.rows = {
                key: row
                for key, row in self._tickets.rows.items()
                if key[0] != repo and row[0] not in run_ids
            }
        if self._runs:
            self._runs.rows = {
                run_id: r for run_id, r in self._runs.rows.items() if r.company != company
            }
        if self._instances:
            self._instances.rows = {
                agent_id: row
                for agent_id, row in self._instances.rows.items()
                if row["company"] != company
            }
        if self._companies:
            self._companies.rows.pop(company, None)
            self._companies.pauses.pop(company, None)
