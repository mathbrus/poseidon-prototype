"""The heartbeat: the standing loop that decides what the company does next.

    heartbeat_config -> heartbeat_observe -> heartbeat_decide (the PM) ->
    heartbeat_authorize -> start child run -> record_tick ->
    sleep -> continue-as-new

One durable workflow per company (`heartbeat-{company}`), kept running by the
worker's ensure loop while the applied blueprint declares a `heartbeat`
section. Each tick re-reads that section from the registry and stops the
loop when it is gone — so revocation is a blueprint diff, effective at the
next tick even if the ensure loop has not noticed yet. The compiled
`heartbeat.start` decision is the second, independent refusal: a tick that
somehow survived the config read still cannot start anything the bundle
does not allow.

The tick's authority is bounded exactly as declared: it starts nothing over
`max_concurrent_runs`, nothing at the `daily_cap`, and only workflows in
`may_start` — each cap checked here for the honest audit reason, and
checked again by policy for the refusal that does not depend on this code.
Every tick outcome (started, idle and why, refused and why) is an audit
event under the `heartbeat-{company}` run id, so the heartbeat's whole story
is one correlated query.

The choice itself is the PM's: `heartbeat_decide` dispatches the standing
conductor with the whole observation (backlog, delivered-but-unreviewed
PRs, run history, capacity and spend) and gets back one action with a
required rationale — journaled as a `heartbeat.decide` audit event. The PM
proposes and the platform disposes: the action set is closed, the target
must come from the observation, `may_start` and the caps bind at
validation and again at the compiled `heartbeat.start` decision. The PM
still cannot start anything — only this workflow actuates, and only after
policy says yes.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError
from temporalio.workflow import ParentClosePolicy

from orchestrator.workflows.base import (
    DISPATCH_RETRY,
    STEP_RETRY,
    RunWorkflow,
    Timeouts,
    workflow_id,
)
from orchestrator.workflows.implement import ImplementInput
from orchestrator.workflows.review import ReviewInput


@dataclass
class HeartbeatInput:
    """`interval_s` compresses the blueprint's minutes for the mock
    brain's e2e (None keeps the declared cadence); `timeouts` rides into
    the child runs the tick starts, for the same reason the CLI's does."""

    company: str
    interval_s: int | None = None
    timeouts: Timeouts | None = None


@workflow.defn(name="heartbeat")
class HeartbeatWorkflow(RunWorkflow):
    def __init__(self) -> None:
        self._phase = "ticking"

    @workflow.query
    def state(self) -> str:
        return self._phase

    @workflow.run
    async def run(self, input: HeartbeatInput) -> dict[str, Any]:
        timeouts = input.timeouts or Timeouts()
        step = timedelta(seconds=timeouts.step_s)

        # Fail closed on the applied blueprint: no heartbeat section, no
        # heartbeat — the loop completes and the ensure loop will only
        # restart it once a section is applied again.
        config = await workflow.execute_activity(
            "heartbeat_config", start_to_close_timeout=step, retry_policy=STEP_RETRY
        )
        if not config:
            await self._record("stopped", "heartbeat_section_absent", timeouts)
            return {"stopped": "no heartbeat section in the applied blueprint"}

        outcome, rule = "idle", "nothing_startable"
        try:
            observed = await workflow.execute_activity(
                "heartbeat_observe", start_to_close_timeout=step, retry_policy=STEP_RETRY
            )
            if observed["active_runs"] >= int(config["max_concurrent_runs"]):
                rule = "runs_in_flight"
            elif observed["daily_spent_usd"] >= float(config["daily_cap_usd"]):
                # The cap halt is a first-class process-gate event, like
                # the per-run budget stop — "nobody may start work today"
                # must be as visible as "this run is out of money".
                rule = "daily_cap_reached"
                await self.record_gate(
                    workflow_id("heartbeat", input.company),
                    "heartbeat_daily_cap",
                    "halted",
                    timeouts,
                )
            elif not observed["backlog"] and not observed["reviewable_prs"]:
                rule = "backlog_empty"
            else:
                outcome, rule = await self._choose_and_start(
                    input, config, observed, timeouts
                )
        except ActivityError as exc:
            # A refused or failed tick never kills the heartbeat; the
            # refusal is already audited where it happened, and the tick's
            # own record carries the summary.
            outcome = "refused"
            rule = str(exc.cause or exc.message)[:200]

        await self._record(outcome, rule, timeouts)

        self._phase = "sleeping"
        if input.interval_s is not None:
            interval = input.interval_s
        else:
            interval = int(config["interval_minutes"]) * 60
        await asyncio.sleep(interval)
        workflow.continue_as_new(input)

    async def _choose_and_start(
        self,
        input: HeartbeatInput,
        config: dict[str, Any],
        observed: dict[str, Any],
        timeouts: Timeouts,
    ) -> tuple[str, str]:
        """Ask the PM for one action and, if policy allows it, start the
        child run. Returns the tick's (outcome, rule)."""
        step = timedelta(seconds=timeouts.step_s)
        # The run id is minted before the decision: heartbeat_decide
        # registers it as the decision's own run so the PM's LLM call can
        # bind (the gateway rejects an unattributed run), and a started
        # child reclaims the same id — so the decide spend lands on the
        # very run the tick chose to start.
        child_run_id = f"auto-{workflow.uuid4().hex[:8]}"
        decision = await workflow.execute_activity(
            "heartbeat_decide",
            args=[child_run_id, observed, list(config.get("may_start") or [])],
            start_to_close_timeout=timedelta(seconds=timeouts.dispatch_s),
            schedule_to_close_timeout=timedelta(
                seconds=timeouts.dispatch_schedule_to_close_s
            ),
            # Temporal's activity liveness heartbeat, not the company's.
            heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
            retry_policy=DISPATCH_RETRY,
        )
        if decision["action"] == "idle":
            return "idle", "pm_chose_idle"
        # Policy first: the compiled heartbeat.start decision is the
        # authority on caps and startable workflows; a deny is audited by
        # the activity and ends the tick.
        await workflow.execute_activity(
            "heartbeat_authorize",
            args=[
                child_run_id,
                decision["action"],
                observed["active_runs"],
                observed["daily_spent_usd"],
            ],
            start_to_close_timeout=step,
            retry_policy=STEP_RETRY,
        )
        target = int(decision["target"])
        family = decision["action"]
        child: ImplementInput | ReviewInput = (
            ImplementInput(
                run_id=child_run_id, issue_number=target, timeouts=input.timeouts
            )
            if family == "implement"
            else ReviewInput(
                run_id=child_run_id, pr_number=target, timeouts=input.timeouts
            )
        )
        await workflow.start_child_workflow(
            family,
            child,
            id=workflow_id(family, child_run_id),
            # The run outlives the tick: the tick starts work, it does not
            # supervise it — progress and gates belong to the run.
            parent_close_policy=ParentClosePolicy.ABANDON,
        )
        return "started", f"{family}_{target}_as_{child_run_id}"

    async def _record(self, outcome: str, rule: str, timeouts: Timeouts) -> None:
        await workflow.execute_activity(
            "record_tick",
            args=[outcome, rule, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=timeouts.step_s),
            retry_policy=STEP_RETRY,
        )
