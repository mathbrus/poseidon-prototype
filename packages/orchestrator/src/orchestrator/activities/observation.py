"""The observation: what the heartbeat decides from and what the scope
run reads — composed once, rendered once.

Retry safety: `heartbeat_observe` and `scope_observation` read the
registry, the open backlog and the delivered PRs' live state under the
platform identity, and remember a PR the world settled on its run row
(idempotent); safe to retry. Only numbers, titles, URLs and attempt counts
leave here: issue bodies are untrusted input the implement run reads
itself.

The git host lists a new issue up to a minute after creating it, and the
loop reads that listing. The one rule about it: the loop reads its own
writes. Every ticket the company files is on the registry the moment the
gateway created it (`registry.filed_tickets`); an observation reads the
ones filed within `LISTING_LAG_S` by number and completes the listing
with them. Nothing else in the tree waits on, polls or reasons about the
host's lag.

Ticket titles flow into the prompts and are untrusted, like every text an
agent reads. Containment is structural, not textual: a poisoned title
can at most influence *which* observed candidate the PM names — the
action set is closed, the target must be in the observation, and the
start must still win the compiled heartbeat.start decision.
"""

import asyncio
import re
from typing import Any

from temporalio import activity

from common.telemetry import get_logger
from orchestrator.activities.blueprint import company_repo
from orchestrator.activities.documents import (
    BOOTSTRAP_TITLES,
    company_documents,
    read_documents,
    sections,
)
from orchestrator.clients import GatewayClient, GatewayError, pr_settled
from orchestrator.config import WorkerSettings
from orchestrator.workflows.base import heartbeat_run_id
from registry import Attempts, BudgetLedger, Companies, FiledTickets, Runs
from schema.documents import DOCUMENT_NAMES

PR_URL_NUMBER_RE = re.compile(r"(\d+)$")
# The observation is a prompt, not a database dump: enough candidates to
# choose well, few enough to read.
BACKLOG_LIMIT = 20
RECENT_RUNS_LIMIT = 10
# How long after the gateway created a ticket the host's listing may
# still lack it: the window of tickets an observation reads by number.
LISTING_LAG_S = 120


def consumed(attempts: Attempts | None, max_attempts: int) -> bool:
    """Whether a work item — an issue or a delivered PR — is off the
    backlog: a run on it is live, a run concluded it (delivered, or
    reached a verdict), or the blueprint's attempt cap is spent. Nothing
    else consumes: a run that aborted before any conclusion (a timed-out
    model call, a step cap, a cancel) leaves the item for another try,
    as many as the charter allows. `max_attempts: 1` is the one-shot
    company, where every run is the only try."""
    if attempts is None:
        return False
    return attempts.live or attempts.concluded or attempts.count >= max_attempts


def _section(header: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"{header}\n{body}"


def _world(observed: dict[str, Any]) -> list[str]:
    """The observation's sections, composed the same way for every reader:
    the three lists, then the company's documents as they stand on
    `base_ref`. The mock brain parses these section headers and the agent
    host's tests mirror them, so changing one is changing a contract —
    `test_heartbeat_activities` pins the shape."""
    tickets = [f"#{i['number']} — {i['title']}" for i in observed.get("backlog") or []]
    prs = [f"#{p['number']} — {p['url']}" for p in observed.get("reviewable_prs") or []]
    runs = [
        f"{r['run_id']} {r['workflow']} {r['status']}: {r['task']}"
        for r in observed.get("recent_runs") or []
    ]
    return [
        _section("Open tickets:", tickets),
        _section("Delivered pull requests awaiting review:", prs),
        _section("Recent runs:", runs),
        *sections(observed.get("documents") or {}),
    ]


def decision_prompt(observed: dict[str, Any], may_start: list[str]) -> str:
    """The decide dispatch's user message: the whole observation plus the
    closed action set and the two-line reply contract."""
    startable = ", ".join([*may_start, "idle"])
    return "\n\n".join(
        [
            f"Heartbeat tick. Runs in flight: {observed['active_runs']}."
            f" Spent today: ${observed['daily_spent_usd']:.2f}."
            f" Open tickets on the repository: {observed['open_tickets']}.\n"
            f"Startable actions: {startable}",
            *_world(observed),
            "Reply with exactly two lines:\n"
            "DECISION: scope | implement #<n> | review #<n> | idle\n"
            "RATIONALE: one line saying why.",
        ]
    )


def scope_prompt(observed: dict[str, Any]) -> str:
    """The scope dispatch's user message: the same observation, asked to
    yield the next slice of work as tickets."""
    return "\n\n".join(
        [
            "Scope the next slice of work. Runs in flight:"
            f" {observed['active_runs']}. Open tickets on the repository:"
            f" {observed['open_tickets']}.",
            *_world(observed),
        ]
    )


class ObservationActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        ledger: BudgetLedger,
        filed: FiledTickets,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._ledger = ledger
        self._filed = filed
        self._log = get_logger("orchestrator.heartbeat")

    async def _repo(self) -> dict[str, str]:
        """The company's repository, from its applied blueprint."""
        return await asyncio.to_thread(
            company_repo, self._companies, self._settings.company
        )

    @activity.defn(name="heartbeat_observe")
    async def heartbeat_observe(self, max_attempts: int) -> dict[str, Any]:
        """What the tick decides from, read under the heartbeat's own id."""
        return await self._observe(max_attempts, heartbeat_run_id(self._settings.company))

    @activity.defn(name="scope_observation")
    async def scope_observation(self, run_id: str) -> str:
        """The scope run's dispatch text: the observation as of now,
        rendered for the conductor's scope procedure and read under the
        scope run's own id, so the platform reads it makes are attributed
        to the run that asked."""
        spec = await asyncio.to_thread(self._companies.get, self._settings.company) or {}
        observed = await self._observe(int(spec["heartbeat"]["max_attempts"]), run_id)
        return scope_prompt(observed)

    async def _observe(self, max_attempts: int, run_id: str) -> dict[str, Any]:
        """The PM's whole decision basis: in-flight runs, today's spend
        and the run history from the registry; the unconsumed open backlog
        and the delivered-but-unreviewed PRs as the candidate actions; the
        raw open-issue count the scoping cap reads; and the company's
        documents as they stand on `base_ref`. Read under the platform
        identity, like every platform read.

        Consumption is `consumed`: an item with a live run, a concluded
        run, or `max_attempts` runs on it is not offered; one whose runs
        all aborted short of a conclusion is offered again, carrying its
        prior attempt count so the compiled heartbeat.start decision can
        bound the retry independently of this code.

        The registry says which PRs the company delivered and which it has
        reviewed; only the git host knows which of those the world has
        since settled — a human who merged by hand, an external harness
        that closed one. So every candidate's live state is read before it
        is offered: a merged or closed PR is not reviewable, and a review
        of it could only fail at the head ref."""

        def read() -> tuple[
            int, float, dict[int, Attempts], list[dict[str, Any]], list[Any]
        ]:
            company = self._settings.company
            reviews = self._runs.review_attempts(company)
            delivered = []
            for url in self._runs.delivered_pr_urls(company):
                match = PR_URL_NUMBER_RE.search(url)
                if not match:
                    continue
                number = int(match.group(1))
                attempts = reviews.get(number)
                if consumed(attempts, max_attempts):
                    continue
                delivered.append(
                    {
                        "number": number,
                        "url": url,
                        "attempts": attempts.count if attempts else 0,
                    }
                )
            return (
                self._runs.active_count(company),
                self._ledger.spent_today(company),
                self._runs.issue_attempts(company),
                delivered,
                self._runs.recent(company, RECENT_RUNS_LIMIT),
            )

        active_runs, daily_spent, issues, delivered, recent = await asyncio.to_thread(
            read
        )
        reviewable = []
        repo = await self._repo()
        paths = await asyncio.to_thread(
            company_documents, self._companies, self._settings.company
        )
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            documents = await read_documents(gateway, run_id, repo, paths)
            open_issues = await gateway.read_open_issues(run_id, repo["url"])
            # Read your own writes: a ticket the company just filed is on
            # the registry before the host lists it, and a listing without
            # it would have the PM scope the same slice twice or the tick
            # file a bootstrap ticket again.
            listed = {int(i["number"]) for i in open_issues}
            filed = await asyncio.to_thread(
                self._filed.filed_since, repo["url"], LISTING_LAG_S
            )
            for number in filed:
                if number in listed:
                    continue
                issue = await gateway.read_issue(run_id, repo["url"], number)
                if issue.get("state") == "open":
                    open_issues.append(issue)
            for candidate in delivered:
                try:
                    pr = await gateway.read_pr(run_id, repo["url"], candidate["number"])
                except GatewayError as exc:
                    # One unreadable candidate must not blind the whole
                    # tick: skip it like a settled one — not offered now,
                    # read again next time. Everything else observed this
                    # tick still reaches the PM.
                    self._log.info(
                        "delivered pr unreadable, skipped",
                        extra={"number": candidate["number"], "error": str(exc)},
                    )
                    continue
                settled = pr_settled(pr)
                if settled:
                    # Observed once, remembered: the run row keeps what the
                    # world did, and no later tick reads this PR again.
                    await asyncio.to_thread(
                        self._runs.mark_pr_settled,
                        self._settings.company,
                        candidate["number"],
                        settled,
                    )
                    self._log.info(
                        "delivered pr no longer reviewable",
                        extra={"number": candidate["number"], "state": settled},
                    )
                    continue
                reviewable.append(candidate)
        # The bootstrap state of each absent document: `pending` when no
        # ticket of the fixed title is open (the tick files one),
        # `exhausted` when the open one spent every attempt short of a
        # delivery (the loop halts by name rather than scope forever).
        bootstrap: dict[str, list[str]] = {"pending": [], "exhausted": []}
        for name in DOCUMENT_NAMES:
            if documents[name]["present"]:
                continue
            tickets = [
                issues.get(int(i["number"]))
                for i in open_issues
                if i.get("title") == BOOTSTRAP_TITLES[name]
            ]
            if not tickets:
                bootstrap["pending"].append(name)
            elif all(
                a is not None
                and not a.live
                and not a.concluded
                and a.count >= max_attempts
                for a in tickets
            ):
                bootstrap["exhausted"].append(name)
        backlog = sorted(
            (
                {
                    "number": int(issue["number"]),
                    "title": str(issue.get("title") or ""),
                    "url": str(issue.get("url") or ""),
                    "attempts": (
                        attempts.count
                        if (attempts := issues.get(int(issue["number"])))
                        else 0
                    ),
                }
                for issue in open_issues
                if not consumed(issues.get(int(issue["number"])), max_attempts)
            ),
            key=lambda issue: issue["number"],
        )[:BACKLOG_LIMIT]
        observed = {
            "active_runs": active_runs,
            "daily_spent_usd": daily_spent,
            # Every open issue, worked or not: the backlog the scoping cap
            # bounds is what the repository shows, not what is startable.
            "open_tickets": len(open_issues),
            "documents": documents,
            "bootstrap": bootstrap,
            "backlog": backlog,
            "reviewable_prs": reviewable,
            "recent_runs": [
                {
                    "run_id": r.run_id,
                    "workflow": r.workflow,
                    "status": r.status,
                    "task": r.task[:80],
                }
                for r in recent
            ],
        }
        self._log.info(
            "heartbeat tick observed",
            extra={
                "active_runs": active_runs,
                "daily_spent_usd": daily_spent,
                "open_tickets": len(open_issues),
                "backlog": len(backlog),
                "reviewable_prs": len(reviewable),
            },
        )
        return observed

    def all(self) -> list[Any]:
        return [self.heartbeat_observe, self.scope_observation]
