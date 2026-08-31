"""The heartbeat: the standing loop that decides what the company does next.

    heartbeat_config -> heartbeat_observe -> heartbeat_decide (the PM) ->
    heartbeat_authorize -> start child run -> record_tick ->
    wait for a wake or the reconcile backstop -> continue-as-new

One durable workflow per company (`heartbeat-{company}`), kept running by the
worker's ensure loop while the applied blueprint declares a `heartbeat`
section. Each tick re-reads that section from the registry and stops the
loop when it is gone — so revocation is a blueprint diff, effective at the
next tick even if the ensure loop has not noticed yet. The compiled
`heartbeat.start` decision is the second, independent refusal: a tick that
somehow survived the config read still cannot start anything the bundle
does not allow.

Ticks are event-triggered. A `wake` signal — the platform sends one when
a run of the company reaches its end — cuts the wait short, and the
blueprint's `reconcile_minutes` is the backstop bounding how stale the
view of the outside world may grow (an issue filed by hand, a PR merged
by hand: changes nothing in the cluster can see happen). A wake is a
nudge, never a command: the tick it causes observes, decides and is
authorized exactly like any other, and a wake landing mid-tick just runs
one more tick before the next wait — many wakes, one extra tick.

The tick's authority is bounded exactly as declared: it starts nothing over
`max_concurrent_runs`, nothing at the `daily_cap`, nothing on an item that
`max_attempts` runs already took up, and only workflows in `may_start` —
each cap checked here for the honest audit reason, and checked again by
policy for the refusal that does not depend on this code. An applied
section that declares no `max_attempts` refuses every tick with a named
rule until the blueprint is re-applied: no cap is defaulted. Every tick
outcome (started, idle and why, refused and why) is an audit event under
the `heartbeat-{company}` run id, so the heartbeat's whole story is one
correlated query.

What the tick offers is bounded the same way: an issue or a delivered PR
is off the backlog while a run on it is live, once a run concluded it
(delivered, or reached a verdict), or once `max_attempts` runs took it
up. A run that aborted short of any conclusion — a timed-out model call,
a step cap — leaves its item for another try; the run that spends the
last attempt records the exhaustion as a process-gate event and tells the
world on the PR or issue.

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
    """`timeouts` rides into the child runs the tick starts — sizing
    depends on the brain, which only the client knows. `pending_wake`
    carries a wake that landed too close to a continue-as-new for the
    closing run to service; the successor honors it as if it had just
    arrived."""

    company: str
    timeouts: Timeouts | None = None
    pending_wake: bool = False


@workflow.defn(name="heartbeat")
class HeartbeatWorkflow(RunWorkflow):
    def __init__(self) -> None:
        self._phase = "ticking"
        self._woken = False

    @workflow.signal
    def wake(self) -> None:
        """A nudge that something changed the world this loop watches —
        never an instruction. The sleeping wait ends (or the running tick
        is followed by one more), and the tick that results observes,
        decides and is authorized like any other."""
        self._woken = True

    @workflow.query
    def state(self) -> str:
        return self._phase

    @workflow.run
    async def run(self, input: HeartbeatInput) -> dict[str, Any]:
        self._woken = bool(input.pending_wake)
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
            if config.get("max_attempts") is None:
                # An applied section from before the cap existed: refuse
                # the tick by name rather than default a cap nobody wrote
                # down. The next apply of a complete section resumes it.
                raise _TickRefused("max_attempts_undeclared")
            observed = await workflow.execute_activity(
                "heartbeat_observe",
                args=[int(config["max_attempts"])],
                start_to_close_timeout=step,
                retry_policy=STEP_RETRY,
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
        except _TickRefused as refused:
            outcome, rule = "refused", refused.rule
        except ActivityError as exc:
            # A refused or failed tick never kills the heartbeat; the
            # refusal is already audited where it happened, and the tick's
            # own record carries the summary.
            outcome = "refused"
            rule = str(exc.cause or exc.message)[:200]

        await self._record(outcome, rule, timeouts)

        self._phase = "sleeping"
        # A wake that landed during the tick skips the wait: one more tick
        # services it, however many wakes coalesced into the flag. The
        # reset before continue-as-new consumes the wake this run answers
        # for; one arriving between the reset and the continue rides
        # `pending_wake` into the successor instead of being dropped.
        woke_early, self._woken = self._woken, False
        if not woke_early:
            try:
                await workflow.wait_condition(
                    lambda: self._woken,
                    timeout=timedelta(seconds=int(config["reconcile_minutes"]) * 60),
                )
            except TimeoutError:
                pass
            self._woken = False
        workflow.continue_as_new(
            HeartbeatInput(
                company=input.company,
                timeouts=input.timeouts,
                pending_wake=self._woken,
            )
        )

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
        target = int(decision["target"])
        family = decision["action"]
        # The chosen item's prior attempts ride the policy input like the
        # other counts; the decide activity already confined the target
        # to the observation, so the candidate is there.
        candidates = observed["backlog" if family == "implement" else "reviewable_prs"]
        attempts = next(int(c["attempts"]) for c in candidates if c["number"] == target)
        # Policy first: the compiled heartbeat.start decision is the
        # authority on caps and startable workflows; a deny is audited by
        # the activity and ends the tick.
        await workflow.execute_activity(
            "heartbeat_authorize",
            args=[
                child_run_id,
                family,
                observed["active_runs"],
                observed["daily_spent_usd"],
                attempts,
            ],
            start_to_close_timeout=step,
            retry_policy=STEP_RETRY,
        )
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


class _TickRefused(Exception):
    """Internal control flow: the tick declines to start anything, by a
    named rule the tick record carries — the heartbeat keeps ticking."""

    def __init__(self, rule: str) -> None:
        super().__init__(rule)
        self.rule = rule
