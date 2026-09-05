"""The company's eyes: the observation the heartbeat decides on and the
scope run reads, the heartbeat's authorization and its audit trail. Thin
clients, as always — sequencing and the wait between ticks live in the
workflow.

Retry safety, per activity:
    heartbeat_config    — read-only; safe.
    heartbeat_observe   — read-only (registry + the open backlog and the
                          delivered PRs' live state under the platform
                          identity); safe. Only numbers, titles, URLs and
                          attempt counts leave here: issue bodies are
                          untrusted input the implement run reads itself.
    scope_observation   — the same read, rendered as the scope run's
                          dispatch text; safe.
    heartbeat_bootstrap — files the bootstrap ticket for an absent
                          document under the platform identity; not
                          idempotent, so the workflow never retries it
                          (`SINGLE_ATTEMPT`): a filing that fails leaves
                          the next tick to file for what is still absent.
    heartbeat_decide    — dispatches the conductor's decide procedure and polls to
                          terminal; the PM answers 409 while running
                          (re-attach) like every dispatch. The chosen
                          action is validated against the observation and
                          journaled; an invalid decision is non-retryable.
    heartbeat_authorize — evaluates the compiled heartbeat.start decision
                          at OPA and audits it; a re-attempt re-evaluates
                          and re-audits, which is the honest record of what
                          was asked. A deny is non-retryable by construction.
    record_tick         — append-only audit write; a duplicate row is a
                          duplicate heartbeat line, never a wrong one.

Ticket titles flow into the decide prompt and are untrusted, like every
text an agent reads. Containment is structural, not textual: a poisoned
title can at most influence *which* observed candidate the PM names —
the action set is closed, the target must be in the observation, and the
start must still win the compiled heartbeat.start decision.
"""

import asyncio
import re
import secrets
import time
from dataclasses import asdict
from datetime import datetime
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from common.audit import args_digest
from common.identity import LIFECYCLE_PLATFORM, ORCHESTRATOR_ID, PLATFORM_ROLE
from common.policy import HEARTBEAT_START
from common.telemetry import get_logger
from orchestrator.activities.blueprint import company_repo, conductor
from orchestrator.activities.documents import (
    BOOTSTRAP_TITLES,
    DOCUMENTS,
    bootstrap_body,
    company_documents,
    read_documents,
    sections,
)
from orchestrator.audit import record_event
from orchestrator.clients import (
    GatewayClient,
    GatewayError,
    dispatch_and_poll,
    pr_settled,
)
from orchestrator.config import WorkerSettings
from orchestrator.opa import evaluate_decision
from orchestrator.workflows.base import (
    LISTING_SETTLE_S,
    PROFILES,
    TEMPORAL_NAMESPACE,
    workflow_id,
)
from registry import AgentInstances, Attempts, AuditStore, BudgetLedger, Companies, Runs

PR_URL_NUMBER_RE = re.compile(r"(\d+)$")
# The observation is a prompt, not a database dump: enough candidates to
# choose well, few enough to read.
BACKLOG_LIMIT = 20
RECENT_RUNS_LIMIT = 10
RATIONALE_CLIP = 300
# The git host lists a new issue up to a minute after creating it
# (`LISTING_SETTLE_S`, the filing's wait). A filing polls the listing at
# this interval; an observation completes the listing from the tickets
# runs finished within the lag window filed, read by number.
LISTING_POLL_S = 1.0
LISTING_LAG_S = 120
ISSUE_URL_NUMBER_RE = re.compile(r"/issues/(\d+)$")

# The workflow's name, as the registry, the audit ledger and the policy
# input all see it.
WORKFLOW = "heartbeat"
# The decision's own run row. The decide dispatch is an LLM call, and the
# LLM gateway binds every call to a registry run — so the row must exist
# before the PM speaks, or every tick dies `llm_unknown_run`. It carries
# the heartbeat workflow name so the observation ignores it (active_count and
# the attempt aggregates scope to implement/review; recent() filters it —
# `registry.runs.recent` spells the same name), and a started child
# reclaims the id through its own idempotent create_run. The task text is
# a label for humans reading the runs table; nothing parses it.
DECIDE_TASK = "heartbeat decision"


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


def _recently_filed(recent: list[Any]) -> set[int]:
    """The numbers of the tickets filed by runs still running or finished
    within the host's listing lag — a scope run's slice tickets, an
    implement run's architecture request."""
    horizon = time.time() - LISTING_LAG_S
    numbers: set[int] = set()
    for run in recent:
        finished = run.finished_at
        if finished and datetime.fromisoformat(finished).timestamp() < horizon:
            continue
        for url in run.ticket_urls:
            match = ISSUE_URL_NUMBER_RE.search(url)
            if match:
                numbers.add(int(match.group(1)))
    return numbers


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


class HeartbeatActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances,
        ledger: BudgetLedger,
        audit: AuditStore,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._instances = instances
        self._ledger = ledger
        self._audit = audit
        self._log = get_logger("orchestrator.heartbeat")

    def _ledger_run_id(self) -> str:
        return workflow_id(WORKFLOW, self._settings.company)

    async def _repo(self) -> dict[str, str]:
        """The company's repository, from its applied blueprint."""
        return await asyncio.to_thread(
            company_repo, self._companies, self._settings.company
        )

    @activity.defn(name="heartbeat_config")
    async def heartbeat_config(self) -> dict[str, Any] | None:
        """The applied blueprint's heartbeat section plus the deployment's
        timeout profile, or None when no company is applied — the workflow
        stops on None, which is what makes deleting the company effective
        at the next tick regardless of what else still believes in it."""
        spec = await asyncio.to_thread(self._companies.get, self._settings.company) or {}
        section = spec.get("heartbeat")
        if not section:
            return None
        return {**section, "timeouts": asdict(PROFILES[self._settings.timeout_profile])}

    @activity.defn(name="heartbeat_observe")
    async def heartbeat_observe(self, max_attempts: int) -> dict[str, Any]:
        """What the tick decides from, read under the heartbeat's own id."""
        return await self._observe(max_attempts, self._ledger_run_id())

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
                self._runs.recent(RECENT_RUNS_LIMIT),
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
            # Read your own writes: a ticket a run of ours just filed is on
            # its row before the host lists it, and a listing without it
            # would have the PM scope the same slice twice.
            listed = {int(i["number"]) for i in open_issues}
            for number in sorted(_recently_filed(recent) - listed):
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
                        candidate["url"],
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
        for name in DOCUMENTS:
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

    @activity.defn(name="heartbeat_bootstrap")
    async def heartbeat_bootstrap(self, names: list[str]) -> list[str]:
        """File the bootstrap ticket for each named absent document under
        the platform identity — the stewardship crew fulfils it on a later
        tick like any other ticket — and journal the filing. Returns the
        names filed.

        Absence is confirmed first: the git host's reads are eventually
        consistent, and a document that answers present now (a delivery
        merged since, a lagging read) is not filed for. And the host lists
        a new issue a moment after creating it, while the next tick — this
        one's own wake — reads that listing to see the ticket already open;
        the filing therefore returns only once its tickets are listed, or
        after `LISTING_SETTLE_S`, whichever comes first."""
        paths = await asyncio.to_thread(
            company_documents, self._companies, self._settings.company
        )
        repo = await self._repo()
        filed: list[str] = []
        numbers: set[int] = set()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            documents = await read_documents(gateway, self._ledger_run_id(), repo, paths)
            for name in names:
                if documents[name]["present"]:
                    self._log.info(
                        "document present on re-read; nothing filed",
                        extra={"document": name},
                    )
                    continue
                branch = f"feature/{name}-{secrets.token_hex(3)}"
                issue = await gateway.create_issue(
                    self._ledger_run_id(),
                    repo["url"],
                    BOOTSTRAP_TITLES[name],
                    bootstrap_body(name, paths[name], branch),
                )
                filed.append(name)
                numbers.add(int(issue["number"]))
                await self._emit(
                    run_id=self._ledger_run_id(),
                    tool="heartbeat.bootstrap",
                    digest=args_digest({"document": name, "path": paths[name]}),
                    decision="allow",
                    rule=f"bootstrap_filed:{name}",
                )
                self._log.info("bootstrap ticket filed", extra={"document": name})
            deadline = time.monotonic() + LISTING_SETTLE_S
            while numbers:
                listed = await gateway.read_open_issues(
                    self._ledger_run_id(), repo["url"]
                )
                if numbers <= {int(i["number"]) for i in listed}:
                    break
                if time.monotonic() >= deadline:
                    self._log.warning(
                        "bootstrap tickets not listed yet",
                        extra={"numbers": sorted(numbers)},
                    )
                    break
                await asyncio.sleep(LISTING_POLL_S)
        return filed

    @activity.defn(name="heartbeat_decide")
    async def heartbeat_decide(
        self,
        run_id: str,
        observed: dict[str, Any],
        may_start: list[str],
        max_open_tickets: int,
    ) -> dict[str, Any]:
        """Dispatch the conductor's decide procedure with the observation
        and validate the answer. The PM proposes; the platform disposes: the
        action must be in `may_start`, the target must be in the
        observation, scoping stops at the open-ticket cap, and the
        rationale is journaled either way — a
        `heartbeat.decide` audit event is the append-only record of *why*
        the company did what it did. The dispatch runs under `run_id` (the
        run the tick would start), so the decision's own LLM spend is
        attributed to the work it chose."""

        def address() -> str:
            spec = self._companies.get(self._settings.company) or {}
            standing_name = (spec.get("heartbeat") or {}).get("conductor")
            if not standing_name:
                raise RuntimeError(
                    "the applied blueprint declares no heartbeat conductor"
                )
            return conductor(self._instances, self._settings.company, standing_name)[
                "url"
            ]

        conductor_url = await asyncio.to_thread(address)
        # Register the decision run before the PM's LLM call can bind to it.
        await asyncio.to_thread(
            self._runs.start,
            run_id,
            DECIDE_TASK,
            WORKFLOW,
            self._ledger_run_id(),
            self._settings.company,
        )
        terminal, terminal_error = "done", None
        try:
            prompt = decision_prompt(observed, may_start)
            status = await dispatch_and_poll(conductor_url, run_id, "decide", prompt)
            decision = status.get("decision")
            if status["status"] != "done" or not decision:
                raise ApplicationError(
                    f"decide dispatch failed: {status.get('error') or 'no decision'}",
                    non_retryable=True,
                )
            problem = self._invalid_reason(
                decision, observed, may_start, max_open_tickets
            )
            if not problem and decision.get("action") == "implement":
                # The listing the observation read may lag a closure by a
                # beat; the ticket is read by number before a run is spent
                # on it.
                problem = await self._closed_ticket(run_id, int(decision["target"]))
            rationale = str(decision.get("rationale") or "")[:RATIONALE_CLIP]
            await self._emit(
                run_id=self._ledger_run_id(),
                tool="heartbeat.decide",
                digest=args_digest(decision),
                decision="deny" if problem else "allow",
                rule=problem or rationale,
            )
            if problem:
                raise ApplicationError(f"decision refused: {problem}", non_retryable=True)
            self._log.info("heartbeat decision", extra=decision)
            return decision
        except BaseException as exc:
            terminal, terminal_error = "failed", str(exc)[:RATIONALE_CLIP]
            raise
        finally:
            # Tidy the decision row on every exit. A started child reclaims
            # the id through its own create_run before it runs, so this
            # finalize never races the run the tick chose — the workflow
            # sequences decide → authorize → start, all after this returns.
            await asyncio.to_thread(
                self._runs.finish, run_id, terminal, error=terminal_error
            )

    async def _closed_ticket(self, run_id: str, number: int) -> str | None:
        repo = (await self._repo())["url"]
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issue = await gateway.read_issue(run_id, repo, number)
        if issue.get("state") == "open":
            return None
        return f"ticket #{number} is {issue.get('state') or 'gone'}"

    @staticmethod
    def _invalid_reason(
        decision: dict[str, Any],
        observed: dict[str, Any],
        may_start: list[str],
        max_open_tickets: int,
    ) -> str | None:
        """Structural containment for the decide reply: a closed action
        set, targets only from the observation, no scoping before the
        documents exist or at the cap. A poisoned ticket title or document
        can steer the choice between candidates, never mint a new one or
        flood the backlog."""
        action, target = decision.get("action"), decision.get("target")
        if action == "idle":
            return None
        if action not in may_start:
            return f"action {action!r} is not in may_start"
        if action == "scope":
            absent = [
                name
                for name, document in (observed.get("documents") or {}).items()
                if not document.get("present")
            ]
            if absent:
                return (
                    f"scope refused: {', '.join(sorted(absent))} absent on the base ref"
                )
            if int(observed["open_tickets"]) >= max_open_tickets:
                return (
                    f"scope refused: {observed['open_tickets']} open tickets"
                    f" reach max_open_tickets={max_open_tickets}"
                )
            return None
        candidates = {
            "implement": {i["number"] for i in observed.get("backlog") or []},
            "review": {p["number"] for p in observed.get("reviewable_prs") or []},
        }.get(action, set())
        if target not in candidates:
            return f"{action} #{target} is not in the observation"
        return None

    @activity.defn(name="heartbeat_authorize")
    async def heartbeat_authorize(
        self,
        child_run_id: str,
        workflow_name: str,
        active_runs: int,
        daily_spent_usd: float,
        attempts: int,
        open_tickets: int,
    ) -> None:
        """Evaluate the compiled heartbeat.start decision — the caps and the
        startable set bind here as policy, not as this module's opinion.
        The live counts ride the input (the active_spawns idiom): runs in
        flight, today's spend, the runs already made on the target item,
        and the repository's open issues; the decision is audited either
        way; a deny ends the tick."""
        company = self._settings.company
        decision_input = {
            "company": company,
            "agent_id": ORCHESTRATOR_ID,
            "lifecycle": LIFECYCLE_PLATFORM,
            "parent_agent_id": "",
            "role": PLATFORM_ROLE,
            "run_id": child_run_id,
            "workflow": WORKFLOW,
            "tool": HEARTBEAT_START,
            "args": {
                "workflow": workflow_name,
                "active_runs": active_runs,
                "daily_spent_usd": daily_spent_usd,
                "attempts": attempts,
                "open_tickets": open_tickets,
            },
            "run_binding_verified": False,
        }
        started = time.monotonic()
        result = await evaluate_decision(self._settings.opa_url, company, decision_input)
        allowed = bool(result.get("allow"))
        await self._emit(
            run_id=child_run_id,
            tool=HEARTBEAT_START,
            digest=args_digest(decision_input["args"]),
            decision="allow" if allowed else "deny",
            rule=str(result.get("rule") or ""),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if not allowed:
            raise ApplicationError(
                f"heartbeat.start refused by policy rule {result.get('rule')!r}"
                f" for workflow {workflow_name!r}",
                non_retryable=True,
            )

    @activity.defn(name="record_tick")
    async def record_tick(self, outcome: str, rule: str, workflow_id: str) -> None:
        """Every tick leaves a row — started, idle and why, refused and
        why — under the heartbeat's own run id, so the loop's whole story
        is one correlated query. The decision column keeps its allow/deny
        contract (the gate-outcome idiom): a tick that started work is an
        allow; every other outcome declined or was refused to start."""
        await self._emit(
            run_id=self._ledger_run_id(),
            tool="heartbeat.tick",
            digest=args_digest({"outcome": outcome, "rule": rule}),
            decision="allow" if outcome == "started" else "deny",
            rule=f"tick_{outcome}:{rule}",
            workflow=workflow_id,
        )
        self._log.info("heartbeat tick", extra={"outcome": outcome, "rule": rule})

    async def _emit(
        self,
        *,
        run_id: str,
        tool: str,
        digest: str,
        decision: str,
        rule: str,
        latency_ms: int = 0,
        workflow: str = WORKFLOW,
    ) -> None:
        await record_event(
            self._audit,
            run_id=run_id,
            tool=tool,
            digest=digest,
            decision=decision,
            rule=rule,
            company=self._settings.company,
            workflow=workflow,
            latency_ms=latency_ms,
        )

    @activity.defn(name="wake_heartbeat")
    async def wake_heartbeat(self) -> None:
        """Nudge the company's standing heartbeat: a run just changed the
        world it watches. Best-effort by design — a missed nudge costs at
        most one reconcile interval — so every failure is swallowed; this
        can never fail the run that sends it."""
        try:
            client = await Client.connect(
                self._settings.temporal_address, namespace=TEMPORAL_NAMESPACE
            )
            handle = client.get_workflow_handle(
                workflow_id(WORKFLOW, self._settings.company)
            )
            await handle.signal("wake")
        except Exception as exc:
            self._log.info("heartbeat wake skipped", extra={"error": str(exc)})

    def all(self) -> list[Any]:
        return [
            self.heartbeat_config,
            self.heartbeat_observe,
            self.scope_observation,
            self.heartbeat_bootstrap,
            self.heartbeat_decide,
            self.heartbeat_authorize,
            self.record_tick,
            self.wake_heartbeat,
        ]
