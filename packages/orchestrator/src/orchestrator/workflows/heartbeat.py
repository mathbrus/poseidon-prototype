"""The heartbeat: the standing loop that decides what the company does next.

    heartbeat_config -> heartbeat_observe -> [heartbeat_bootstrap] ->
    heartbeat_decide (the PM) -> heartbeat_authorize -> start child run ->
    record_tick -> wait for a wake or the reconcile backstop ->
    continue-as-new

One durable workflow per company (`heartbeat-{company}`), kept running by
the worker's ensure loop for as long as the company is applied. Each tick
re-reads the blueprint's `heartbeat` section from the registry and stops
the loop when the company is gone — deleting the company is how it stops,
effective at the next tick even if the ensure loop has not noticed yet.
The compiled `heartbeat.start` decision is the second, independent
refusal: a tick that somehow survived the config read still cannot start
anything the bundle does not allow.

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
`max_attempts` runs already took up, no scope run at `max_open_tickets`,
and only workflows in `may_start` — each cap checked here for the honest
audit reason, and checked again by policy for the refusal that does not
depend on this code. Every tick outcome (started, idle and why, refused
and why) is an audit event under the `heartbeat-{company}` run id, so the
heartbeat's whole story is one correlated query.

A company begins with its vision alone. A tick that observes a document
absent on the base ref with no bootstrap ticket open files one under the
platform identity and wakes itself, so the next tick offers the
stewardship crew's work like any other ticket; scoping is refused until
both documents exist. A bootstrap ticket whose every attempt aborted
halts the loop by name (the `heartbeat_bootstrap_exhausted` process-gate
event; the tick's rule is `bootstrap_exhausted`) —
never a silent stall: a human closes the ticket and the loop refiles.

What the tick offers is bounded the same way: an issue or a delivered PR
is off the backlog while a run on it is live, once a run concluded it
(delivered, or reached a verdict), or once `max_attempts` runs took it
up. A run that aborted short of any conclusion — a timed-out model call,
a step cap — leaves its item for another try; the run that spends the
last attempt records the exhaustion as a process-gate event and tells the
world on the PR or issue.

A human may hold the loop: `poseidon pause` records the pause under
their name in the registry, and a tick that reads it starts nothing,
observes nothing and asks the PM nothing — it records `tick_paused` with
the name and waits like any other tick, so the loop stays visible
(`heartbeat show` says `paused`) and a resume takes effect at once. Runs
already in flight are not the loop's: they finish on their own. The
compiled `heartbeat.start` decision reads the pause again at authorize
time, so one that lands mid-tick still refuses the start. A pause is not
a stop: the company stays, and deleting it is the one stop.

The choice itself is the PM's: `heartbeat_decide` dispatches the standing
conductor with the whole observation (backlog, delivered-but-unreviewed
PRs, run history, capacity and spend) and gets back one action with a
required rationale — journaled as a `heartbeat.decide` audit event: scope
the next slice, implement a ticket, review a pull request, or idle. The PM
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

from common.workflows import (
    BOOTSTRAP_GATE,
    DAILY_CAP_GATE,
    GATE_HALTED,
    HEARTBEAT_PAUSED,
    HEARTBEAT_SLEEPING,
    HEARTBEAT_TICKING,
    TICK_IDLE,
    TICK_PAUSED,
    TICK_REFUSED,
    TICK_STARTED,
    TICK_STOPPED,
    workflow_id,
)
from orchestrator.workflows.base import (
    BOOTSTRAP_S,
    DISPATCH_RETRY,
    SINGLE_ATTEMPT,
    STEP_RETRY,
    RunWorkflow,
    Timeouts,
)
from orchestrator.workflows.implement import ImplementInput
from orchestrator.workflows.review import ReviewInput
from orchestrator.workflows.scope import ScopeInput


@dataclass
class HeartbeatInput:
    """`pending_wake` carries a wake that landed too close to a
    continue-as-new for the closing run to service; the successor honors it
    as if it had just arrived."""

    company: str
    pending_wake: bool = False


@workflow.defn(name="heartbeat")
class HeartbeatWorkflow(RunWorkflow):
    def __init__(self) -> None:
        self._state = HEARTBEAT_TICKING
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
        """`ticking`, `sleeping`, or `paused` while a human holds the loop."""
        return self._state

    @workflow.run
    async def run(self, input: HeartbeatInput) -> dict[str, Any]:
        self._woken = bool(input.pending_wake)
        # Fail closed on the applied blueprint: no company, no heartbeat —
        # the loop completes, and the ensure loop restarts it only once a
        # company is applied again. The section carries the deployment's
        # timeout profile for everything that follows.
        config = await workflow.execute_activity(
            "heartbeat_config",
            input.company,
            start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
            retry_policy=STEP_RETRY,
        )
        if not config:
            await self._record(TICK_STOPPED, "blueprint_absent")
            return {"stopped": "no applied blueprint for the company"}
        self._profile = Timeouts(**config["timeouts"])

        if config.get("paused"):
            # A human holds the loop: nothing is observed, decided or
            # started; the row says who, and the wait below is the usual
            # one, so a resume (which wakes) or the backstop ends it.
            outcome, rule = TICK_PAUSED, str(config["paused"]["by"])
        else:
            outcome, rule = await self._tick(input, config)
        await self._record(outcome, rule)

        self._state = HEARTBEAT_PAUSED if outcome == TICK_PAUSED else HEARTBEAT_SLEEPING
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
                pending_wake=self._woken,
            )
        )

    async def _tick(
        self, input: HeartbeatInput, config: dict[str, Any]
    ) -> tuple[str, str]:
        """One look at the world and, when there is something to do and
        policy allows it, one child run started. Returns the tick's
        (outcome, rule)."""
        step = timedelta(seconds=self._profile.step_s)
        outcome, rule = TICK_IDLE, ""
        try:
            observed = await workflow.execute_activity(
                "heartbeat_observe",
                args=[int(config["max_attempts"])],
                start_to_close_timeout=step,
                retry_policy=STEP_RETRY,
            )
            bootstrap = observed.get("bootstrap") or {}
            if bootstrap.get("pending"):
                # Filing is not idempotent: never retried. A filing that
                # fails refuses this tick, and the next one files only for
                # what is still absent.
                filed = await workflow.execute_activity(
                    "heartbeat_bootstrap",
                    args=[list(bootstrap["pending"])],
                    start_to_close_timeout=step,
                    retry_policy=SINGLE_ATTEMPT,
                )
                # The filing re-reads the documents; one that answered
                # present since is not filed for and is read again next.
                rule = (
                    "bootstrap_filed:" + ",".join(filed)
                    if filed
                    else "bootstrap_present:" + ",".join(bootstrap["pending"])
                )
                # The ticket is the next tick's to offer: skip the wait.
                self._woken = True
            elif bootstrap.get("exhausted"):
                # Every attempt on the bootstrap ticket aborted short of a
                # delivery: the company cannot write its own documents and
                # says so, as a first-class process-gate event.
                rule = "bootstrap_exhausted:" + ",".join(bootstrap["exhausted"])
                await self.record_gate(
                    workflow_id("heartbeat", input.company),
                    BOOTSTRAP_GATE,
                    GATE_HALTED,
                )
            elif observed["active_runs"] >= int(config["max_concurrent_runs"]):
                rule = "runs_in_flight"
            elif observed["daily_spent_usd"] >= float(config["daily_cap_usd"]):
                # The cap halt is a first-class process-gate event, like
                # the per-run budget park — "nobody may start work today"
                # must be as visible as "this run is out of money".
                rule = "daily_cap_reached"
                await self.record_gate(
                    workflow_id("heartbeat", input.company),
                    DAILY_CAP_GATE,
                    GATE_HALTED,
                )
            elif not self._anything_startable(config, observed):
                rule = "backlog_empty"
            else:
                outcome, rule = await self._choose_and_start(input, config, observed)
        except ActivityError as exc:
            # A refused or failed tick never kills the heartbeat; the
            # refusal is already audited where it happened, and the tick's
            # own record carries the summary.
            outcome = TICK_REFUSED
            rule = str(exc.cause or exc.message)[:200]
        return outcome, rule

    @staticmethod
    def _anything_startable(config: dict[str, Any], observed: dict[str, Any]) -> bool:
        """Whether the tick has anything to ask the PM about: a ticket to
        implement, a delivered PR to review, or room to scope the next
        slice. An empty world never wakes the PM."""
        scope_open = "scope" in (config.get("may_start") or []) and int(
            observed["open_tickets"]
        ) < int(config["max_open_tickets"])
        return bool(observed["backlog"] or observed["reviewable_prs"] or scope_open)

    async def _choose_and_start(
        self,
        input: HeartbeatInput,
        config: dict[str, Any],
        observed: dict[str, Any],
    ) -> tuple[str, str]:
        """Ask the PM for one action and, if policy allows it, start the
        child run. Returns the tick's (outcome, rule)."""
        step = timedelta(seconds=self._profile.step_s)
        # The run id is minted before the decision: heartbeat_decide
        # registers it as the decision's own run so the PM's LLM call can
        # bind (the gateway rejects an unattributed run), and a started
        # child reclaims the same id — so the decide spend lands on the
        # very run the tick chose to start.
        child_run_id = f"auto-{workflow.uuid4().hex[:8]}"
        decision = await workflow.execute_activity(
            "heartbeat_decide",
            args=[
                child_run_id,
                observed,
                list(config.get("may_start") or []),
                int(config["max_open_tickets"]),
            ],
            start_to_close_timeout=timedelta(seconds=self._profile.dispatch_s),
            schedule_to_close_timeout=timedelta(
                seconds=self._profile.dispatch_schedule_to_close_s
            ),
            # Temporal's activity liveness heartbeat, not the company's.
            heartbeat_timeout=timedelta(seconds=self._profile.dispatch_heartbeat_s),
            retry_policy=DISPATCH_RETRY,
        )
        action = decision["action"]
        if action == "idle":
            return TICK_IDLE, "pm_chose_idle"
        target = decision.get("target")
        # The chosen item's prior attempts ride the policy input like the
        # other counts; the decide activity already confined the target
        # to the observation, so the candidate is there. A scope run works
        # no item and carries no attempts.
        attempts = 0
        if action != "scope":
            candidates = observed[
                "backlog" if action == "implement" else "reviewable_prs"
            ]
            attempts = next(
                int(c["attempts"]) for c in candidates if c["number"] == target
            )
        # Policy first: the compiled heartbeat.start decision is the
        # authority on caps and startable workflows; a deny is audited by
        # the activity and ends the tick.
        await workflow.execute_activity(
            "heartbeat_authorize",
            args=[
                child_run_id,
                action,
                observed["active_runs"],
                observed["daily_spent_usd"],
                attempts,
                observed["open_tickets"],
            ],
            start_to_close_timeout=step,
            retry_policy=STEP_RETRY,
        )
        child: ImplementInput | ReviewInput | ScopeInput
        if action == "scope":
            child = ScopeInput(run_id=child_run_id)
        elif action == "implement":
            child = ImplementInput(run_id=child_run_id, issue_number=int(target))
        else:
            child = ReviewInput(run_id=child_run_id, pr_number=int(target))
        await workflow.start_child_workflow(
            action,
            child,
            id=workflow_id(action, child_run_id),
            # The run outlives the tick: the tick starts work, it does not
            # supervise it — progress and gates belong to the run.
            parent_close_policy=ParentClosePolicy.ABANDON,
        )
        item = "" if action == "scope" else f"_{target}"
        return TICK_STARTED, f"{action}{item}_as_{child_run_id}"

    async def _record(self, outcome: str, rule: str) -> None:
        await workflow.execute_activity(
            "record_tick",
            args=[outcome, rule, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=STEP_RETRY,
        )
