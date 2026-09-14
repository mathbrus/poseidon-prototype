"""The heartbeat's own activities: the loop's config, the PM's decision
and its containment, the compiled heartbeat.start decision, the tick's
audit row and the wake. The observation the loop decides on lives in
`observation`; the bootstrap filing in `bootstrap`.

Retry safety, per activity:
    heartbeat_config    — read-only; safe.
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
"""

import asyncio
import time
from dataclasses import asdict
from typing import Any

from temporalio import activity
from temporalio.client import Client
from temporalio.exceptions import ApplicationError

from common.audit import HEARTBEAT_DECIDE, RATIONALE_LIMIT, args_digest
from common.identity import LIFECYCLE_PLATFORM, ORCHESTRATOR_ID, PLATFORM_ROLE
from common.opa import evaluate_decision
from common.policy import HEARTBEAT_START, HEARTBEAT_TICK
from common.telemetry import get_logger
from common.workflows import TEMPORAL_NAMESPACE, heartbeat_run_id, workflow_id
from orchestrator.activities.blueprint import conductor
from orchestrator.activities.company import applied_company
from orchestrator.activities.observation import decision_prompt
from orchestrator.audit import record_event
from orchestrator.clients import dispatch_and_poll
from orchestrator.config import WorkerSettings
from orchestrator.workflows.base import PROFILES
from registry import AgentInstances, AuditStore, Companies, Runs

# The run row's error column is a label for a human reading the runs
# table, not a log; a failed decision's message is cut to fit.
ERROR_CLIP = 300
# The row a valid decision writes: `rule` names that the platform's
# structural checks passed, the rationale beside it is the PM's own.
DECISION_VALID = "decision_valid"

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


async def emit_heartbeat_event(
    audit: AuditStore,
    company: str,
    *,
    run_id: str,
    tool: str,
    digest: str,
    decision: str,
    rule: str,
    rationale: str | None = None,
    latency_ms: int = 0,
    workflow: str = WORKFLOW,
) -> None:
    """One row of the heartbeat's story: what the loop decided, filed,
    authorized or did on a tick, under the company's heartbeat run id."""
    await record_event(
        audit,
        run_id=run_id,
        tool=tool,
        digest=digest,
        decision=decision,
        rule=rule,
        rationale=rationale,
        company=company,
        workflow=workflow,
        latency_ms=latency_ms,
    )


class HeartbeatActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances,
        audit: AuditStore,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._instances = instances
        self._audit = audit
        self._log = get_logger("orchestrator.heartbeat")

    def _company(self) -> str:
        return applied_company(self._companies)

    @activity.defn(name="heartbeat_config")
    async def heartbeat_config(self, company: str) -> dict[str, Any] | None:
        """The applied blueprint's heartbeat section plus the deployment's
        timeout profile, or None when `company` — the one this heartbeat
        beats for — is not the applied one: deleted, or replaced by another
        name. The workflow stops on None, which is what makes deleting the
        company effective at the next tick regardless of what else still
        believes in it."""
        if await asyncio.to_thread(self._companies.applied) != company:
            return None
        spec = await asyncio.to_thread(self._companies.get, company) or {}
        section = spec.get("heartbeat")
        if not section:
            return None
        return {**section, "timeouts": asdict(PROFILES[self._settings.timeout_profile])}

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
            spec = self._companies.get(self._company()) or {}
            standing_name = (spec.get("heartbeat") or {}).get("conductor")
            if not standing_name:
                raise RuntimeError(
                    "the applied blueprint declares no heartbeat conductor"
                )
            return conductor(self._instances, self._company(), standing_name)["url"]

        conductor_url = await asyncio.to_thread(address)
        # Register the decision run before the PM's LLM call can bind to it.
        await asyncio.to_thread(
            self._runs.start,
            run_id,
            DECIDE_TASK,
            WORKFLOW,
            heartbeat_run_id(self._company()),
            self._company(),
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
            refusal = self._invalid_reason(
                decision, observed, may_start, max_open_tickets
            )
            # The row: a refusal is named by its rule and owes no
            # rationale; a valid decision carries the PM's own reason.
            await self._emit(
                run_id=heartbeat_run_id(self._company()),
                tool=HEARTBEAT_DECIDE,
                digest=args_digest(decision),
                decision="deny" if refusal else "allow",
                rule=refusal[0] if refusal else DECISION_VALID,
                rationale=None
                if refusal
                else str(decision.get("rationale") or "")[:RATIONALE_LIMIT],
            )
            if refusal:
                raise ApplicationError(
                    f"decision refused: {refusal[1]}", non_retryable=True
                )
            self._log.info("heartbeat decision", extra=decision)
            return decision
        except BaseException as exc:
            terminal, terminal_error = "failed", str(exc)[:ERROR_CLIP]
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
        decision: dict[str, Any],
        observed: dict[str, Any],
        may_start: list[str],
        max_open_tickets: int,
    ) -> tuple[str, str] | None:
        """Structural containment for the decide reply: a closed action
        set, targets only from the observation, no scoping before the
        documents exist or at the cap. A poisoned ticket title or document
        can steer the choice between candidates, never mint a new one or
        flood the backlog. A refusal is `(rule, message)`: the rule names
        it on the audit row, the message says it to a reader of the run."""
        action, target = decision.get("action"), decision.get("target")
        if action == "idle":
            return None
        if action not in may_start:
            return "not_in_may_start", f"action {action!r} is not in may_start"
        if action == "scope":
            absent = [
                name
                for name, document in (observed.get("documents") or {}).items()
                if not document.get("present")
            ]
            if absent:
                return (
                    "documents_absent",
                    f"scope refused: {', '.join(sorted(absent))} absent on the base ref",
                )
            if int(observed["open_tickets"]) >= max_open_tickets:
                return (
                    "max_open_tickets_reached",
                    f"scope refused: {observed['open_tickets']} open tickets"
                    f" reach max_open_tickets={max_open_tickets}",
                )
            return None
        candidates = {
            "implement": {i["number"] for i in observed.get("backlog") or []},
            "review": {p["number"] for p in observed.get("reviewable_prs") or []},
        }.get(action, set())
        if target not in candidates:
            return "target_not_observed", f"{action} #{target} is not in the observation"
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
        company = self._company()
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
            run_id=heartbeat_run_id(self._company()),
            tool=HEARTBEAT_TICK,
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
        rationale: str | None = None,
        latency_ms: int = 0,
        workflow: str = WORKFLOW,
    ) -> None:
        await emit_heartbeat_event(
            self._audit,
            self._company(),
            run_id=run_id,
            tool=tool,
            digest=digest,
            decision=decision,
            rule=rule,
            rationale=rationale,
            latency_ms=latency_ms,
            workflow=workflow,
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
            handle = client.get_workflow_handle(workflow_id(WORKFLOW, self._company()))
            await handle.signal("wake")
        except Exception as exc:
            self._log.info("heartbeat wake skipped", extra={"error": str(exc)})

    def all(self) -> list[Any]:
        return [
            self.heartbeat_config,
            self.heartbeat_decide,
            self.heartbeat_authorize,
            self.record_tick,
            self.wake_heartbeat,
        ]
