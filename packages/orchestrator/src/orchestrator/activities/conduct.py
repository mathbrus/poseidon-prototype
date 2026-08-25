"""The conduct activities: the heartbeat's eyes, its authorization and its
audit trail. Thin clients, as always — sequencing and the tick cadence live
in the workflow.

Retry safety, per activity:
    conduct_config      — read-only; safe.
    conduct_observe     — read-only (registry + the open backlog under the
                          platform identity); safe. Only numbers, titles
                          and URLs leave here: issue bodies are untrusted
                          input the implement run reads itself.
    conduct_decide      — dispatches the PM's decide mode and polls to
                          terminal; the PM answers 409 while running
                          (re-attach) like every dispatch. The chosen
                          action is validated against the observation and
                          journaled; an invalid decision is non-retryable.
    conduct_authorize   — evaluates the compiled conduct.start decision at
                          OPA and audits it; a re-attempt re-evaluates and
                          re-audits, which is the honest record of what was
                          asked. A deny is non-retryable by construction.
    record_conduct_tick — append-only audit write; a duplicate row is a
                          duplicate heartbeat line, never a wrong one.

Ticket titles flow into the decide prompt and are untrusted, like every
text an agent reads. Containment is structural, not textual: a poisoned
title can at most influence *which* observed candidate the PM names —
the action set is closed, the target must be in the observation, and the
start must still win the compiled conduct.start decision.
"""

import asyncio
import re
import time
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from common.audit import AuditEvent, args_digest
from common.telemetry import get_logger
from orchestrator.clients import AgentClient, GatewayClient
from orchestrator.config import WorkerSettings
from orchestrator.opa import evaluate_decision
from registry import AuditStore, BudgetLedger, Runs
from registry.bundles import Companies

PR_URL_NUMBER_RE = re.compile(r"(\d+)$")
# The observation is a prompt, not a database dump: enough candidates to
# choose well, few enough to read.
BACKLOG_LIMIT = 20
RECENT_RUNS_LIMIT = 10
RATIONALE_CLIP = 300

# The decision's own run row. The decide dispatch is an LLM call, and the
# LLM gateway binds every call to a registry run — so the row must exist
# before the PM speaks, or every tick dies `llm_unknown_run`. It carries
# the `conduct` workflow so the observation ignores it (active_count and
# started_issue_numbers scope to implement/review; recent() filters it),
# and a started child reclaims the id through its own idempotent create_run.
DECIDE_WORKFLOW = "conduct"
DECIDE_TASK = "conduct decision"


def _section(header: str, lines: list[str]) -> str:
    body = "\n".join(lines) if lines else "(none)"
    return f"{header}\n{body}"


def decision_prompt(observed: dict[str, Any], may_start: list[str]) -> str:
    """The decide dispatch's user message: the whole observation, composed
    the same way every tick. The mock brain parses these section headers;
    changing them is changing a contract."""
    startable = ", ".join([*may_start, "idle"])
    tickets = [f"#{i['number']} — {i['title']}" for i in observed.get("backlog") or []]
    prs = [f"#{p['number']} — {p['url']}" for p in observed.get("reviewable_prs") or []]
    runs = [
        f"{r['run_id']} {r['workflow']} {r['status']}: {r['task']}"
        for r in observed.get("recent_runs") or []
    ]
    return "\n\n".join(
        [
            f"Conduct tick. Runs in flight: {observed['active_runs']}."
            f" Spent today: ${observed['daily_spent_usd']:.2f}.\n"
            f"Startable actions: {startable}",
            _section("Open tickets:", tickets),
            _section("Delivered pull requests awaiting review:", prs),
            _section("Recent runs:", runs),
            "Reply with exactly two lines:\n"
            "DECISION: implement #<n> | review #<n> | idle\n"
            "RATIONALE: one line saying why.",
        ]
    )


class ConductActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        ledger: BudgetLedger,
        audit: AuditStore,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._ledger = ledger
        self._audit = audit
        self._log = get_logger("orchestrator.conduct")

    def _conduct_run_id(self) -> str:
        from orchestrator.workflows.conduct import conduct_workflow_id

        return conduct_workflow_id(self._settings.company)

    @activity.defn(name="conduct_config")
    async def conduct_config(self) -> dict[str, Any] | None:
        """The applied blueprint's conduct section, or None — the workflow
        stops on None, which is what makes removal effective at the next
        tick regardless of what else still believes in the heartbeat."""
        spec = await asyncio.to_thread(self._companies.get, self._settings.company) or {}
        return spec.get("conduct") or None

    @activity.defn(name="conduct_observe")
    async def conduct_observe(self) -> dict[str, Any]:
        """What the tick decides from — the PM's whole decision basis:
        in-flight runs, today's spend and the run history from the
        registry; the unconsumed open backlog and the delivered-but-
        unreviewed PRs as the candidate actions. Read under the platform
        identity, like every platform read."""

        def read() -> tuple[int, float, set[int], list[dict[str, Any]], list[Any]]:
            company = self._settings.company
            reviewed = self._runs.reviewed_pr_numbers(company)
            reviewable = []
            for url in self._runs.delivered_pr_urls(company):
                match = PR_URL_NUMBER_RE.search(url)
                if match and int(match.group(1)) not in reviewed:
                    reviewable.append({"number": int(match.group(1)), "url": url})
            return (
                self._runs.active_count(company),
                self._ledger.spent_today(company),
                self._runs.started_issue_numbers(company),
                reviewable,
                self._runs.recent(RECENT_RUNS_LIMIT),
            )

        active_runs, daily_spent, started, reviewable, recent = await asyncio.to_thread(
            read
        )
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issues = await gateway.read_open_issues(
                self._conduct_run_id(), self._settings.target_repo
            )
        backlog = sorted(
            (
                {
                    "number": int(issue["number"]),
                    "title": str(issue.get("title") or ""),
                    "url": str(issue.get("url") or ""),
                }
                for issue in issues
                if int(issue["number"]) not in started
            ),
            key=lambda issue: issue["number"],
        )[:BACKLOG_LIMIT]
        observed = {
            "active_runs": active_runs,
            "daily_spent_usd": daily_spent,
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
            "conduct tick observed",
            extra={
                "active_runs": active_runs,
                "daily_spent_usd": daily_spent,
                "backlog": len(backlog),
                "reviewable_prs": len(reviewable),
            },
        )
        return observed

    @activity.defn(name="conduct_decide")
    async def conduct_decide(
        self, run_id: str, observed: dict[str, Any], may_start: list[str]
    ) -> dict[str, Any]:
        """Dispatch the PM's decide mode with the observation and validate
        the answer. The PM proposes; the platform disposes: the action must
        be in `may_start`, the target must be in the observation, and the
        rationale is journaled either way — a `conduct.decide` audit event
        is the append-only record of *why* the company did what it did.
        The dispatch runs under `run_id` (the run the tick would start), so
        the decision's own LLM spend is attributed to the work it chose."""
        if not self._settings.pm_url:
            raise RuntimeError("PM_URL is not configured on the worker")
        # Register the decision run before the PM's LLM call can bind to it.
        await asyncio.to_thread(
            self._runs.start,
            run_id,
            DECIDE_TASK,
            DECIDE_WORKFLOW,
            self._conduct_run_id(),
            self._settings.company,
        )
        terminal, terminal_error = "done", None
        try:
            prompt = decision_prompt(observed, may_start)
            async with AgentClient(self._settings.pm_url) as agent:
                await agent.dispatch(run_id, prompt, mode="decide")
                while True:
                    if activity.in_activity():
                        activity.heartbeat()
                    status = await agent.status(run_id)
                    if status["status"] != "running":
                        break
                    await asyncio.sleep(self._settings.poll_interval_s)
            decision = status.get("decision")
            if status["status"] != "done" or not decision:
                raise ApplicationError(
                    f"decide dispatch failed: {status.get('error') or 'no decision'}",
                    non_retryable=True,
                )
            problem = self._invalid_reason(decision, observed, may_start)
            rationale = str(decision.get("rationale") or "")[:RATIONALE_CLIP]
            await self._emit(
                run_id=self._conduct_run_id(),
                tool="conduct.decide",
                digest=args_digest(decision),
                decision="deny" if problem else "allow",
                rule=problem or rationale,
            )
            if problem:
                raise ApplicationError(f"decision refused: {problem}", non_retryable=True)
            self._log.info("conduct decision", extra=decision)
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

    @staticmethod
    def _invalid_reason(
        decision: dict[str, Any], observed: dict[str, Any], may_start: list[str]
    ) -> str | None:
        """Structural containment for the decide reply: a closed action
        set, targets only from the observation. A poisoned ticket title can
        steer the choice between candidates, never mint a new one."""
        action, target = decision.get("action"), decision.get("target")
        if action == "idle":
            return None
        if action not in may_start:
            return f"action {action!r} is not in may_start"
        candidates = {
            "implement": {i["number"] for i in observed.get("backlog") or []},
            "review": {p["number"] for p in observed.get("reviewable_prs") or []},
        }.get(action, set())
        if target not in candidates:
            return f"{action} #{target} is not in the observation"
        return None

    @activity.defn(name="conduct_authorize")
    async def conduct_authorize(
        self,
        child_run_id: str,
        workflow_name: str,
        active_runs: int,
        daily_spent_usd: float,
    ) -> None:
        """Evaluate the compiled conduct.start decision — the caps and the
        startable set bind here as policy, not as this module's opinion.
        The live counts ride the input (the active_spawns idiom); the
        decision is audited either way; a deny ends the tick."""
        company = self._settings.company
        decision_input = {
            "company": company,
            "agent_id": "orchestrator",
            "lifecycle": "platform",
            "parent_agent_id": "",
            "role": "platform",
            "run_id": child_run_id,
            "workflow": "conduct",
            "tool": "conduct.start",
            "args": {
                "workflow": workflow_name,
                "active_runs": active_runs,
                "daily_spent_usd": daily_spent_usd,
            },
            "run_binding_verified": False,
        }
        started = time.monotonic()
        result = await evaluate_decision(self._settings.opa_url, company, decision_input)
        allowed = bool(result.get("allow"))
        await self._emit(
            run_id=child_run_id,
            tool="conduct.start",
            digest=args_digest(decision_input["args"]),
            decision="allow" if allowed else "deny",
            rule=str(result.get("rule") or ""),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if not allowed:
            raise ApplicationError(
                f"conduct.start refused by policy rule {result.get('rule')!r}"
                f" for workflow {workflow_name!r}",
                non_retryable=True,
            )

    @activity.defn(name="record_conduct_tick")
    async def record_conduct_tick(
        self, outcome: str, rule: str, workflow_id: str
    ) -> None:
        """Every tick leaves a row — started, idle and why, refused and
        why — under the heartbeat's own run id, so the loop's whole story
        is one correlated query. The decision column keeps its allow/deny
        contract (the gate-outcome idiom): a tick that started work is an
        allow; every other outcome declined or was refused to start."""
        await self._emit(
            run_id=self._conduct_run_id(),
            tool="conduct.tick",
            digest=args_digest({"outcome": outcome, "rule": rule}),
            decision="allow" if outcome == "started" else "deny",
            rule=f"tick_{outcome}:{rule}",
            workflow=workflow_id,
        )
        self._log.info("conduct tick", extra={"outcome": outcome, "rule": rule})

    async def _emit(
        self,
        *,
        run_id: str,
        tool: str,
        digest: str,
        decision: str,
        rule: str,
        latency_ms: int = 0,
        workflow: str = "conduct",
    ) -> None:
        event = AuditEvent(
            run_id=run_id,
            agent_id="orchestrator",
            role="platform",
            tool=tool,
            args_digest=digest,
            decision=decision,
            rule=rule,
            latency_ms=latency_ms,
            backend_status=None,
            source="process-lifecycle",
            company=self._settings.company,
            lifecycle="platform",
            parent_agent_id="",
            workflow=workflow,
        )
        await asyncio.to_thread(self._audit.append, event)

    def all(self) -> list[Any]:
        return [
            self.conduct_config,
            self.conduct_observe,
            self.conduct_decide,
            self.conduct_authorize,
            self.record_conduct_tick,
        ]
