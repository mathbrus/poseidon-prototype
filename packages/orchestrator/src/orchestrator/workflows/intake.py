"""The intake workflow: goal text in, tickets and
recorded crew intent out.

    create_intake_run -> check_budget -> [gate slot] -> dispatch_pm ->
    finalize_intake

The PM does the scoping itself, through governed tools: it files tickets
via `issues.*` and records the `crew.request` for feature-delivery under
its own conductor authority — the recorded, expiring request a subsequent
implement run consumes (the intake -> implement handoff). The workflow
never touches GitHub or the registry's crew tables directly.

Intake is the injection surface for the standing conductor: the goal text
is untrusted input handed straight to the PM. Containment is the compiled
policy's (the PM cannot execute, deliver, or spawn outside its may_spawn
list) — the injected-conductor red-team test enters here.

No human gate by default — but the slot exists and reads the blueprint's
`workflows.intake.gates` like any other gate: declare
`gates.intake.resolved_by: human` and the workflow parks on the `intake`
signal before dispatching the PM.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from orchestrator.gates import Gate
from orchestrator.workflows.base import (
    DISPATCH_RETRY,
    STEP_RETRY,
    TEARDOWN_RETRY,
    RunWorkflow,
    Timeouts,
)


@dataclass
class IntakeInput:
    run_id: str
    goal_text: str
    timeouts: Timeouts | None = None


@workflow.defn(name="intake")
class IntakeWorkflow(RunWorkflow):
    def __init__(self) -> None:
        self._phase = "running"
        self._intake_gate = Gate("intake")

    @workflow.signal
    def intake(self, approved: bool, payload: str | None = None) -> None:
        """`poseidon signal <run_id> intake --approved/--no-approved` —
        resolves the gate when the blueprint configures one."""
        self._intake_gate.resolve(approved, payload)

    @workflow.query
    def state(self) -> str:
        return self._phase

    @workflow.run
    async def run(self, input: IntakeInput) -> dict[str, Any]:
        timeouts = input.timeouts or Timeouts()
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            await workflow.execute_activity(
                "create_intake_run",
                args=[input.run_id, input.goal_text],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            await workflow.execute_activity(
                "check_budget",
                args=[input.run_id],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            gates = await workflow.execute_activity(
                "intake_gate_config",
                args=[],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            if ((gates or {}).get("intake") or {}).get("resolved_by") == "human":
                # The configured gate: park until a human answers or the
                # escalation deadline lapses. Transitions are
                # process-gate audit events like every other gate's.
                self._phase = "paused-intake_gate"
                outcome = await self._intake_gate.wait(
                    timeout=timedelta(seconds=timeouts.budget_gate_s),
                    escalate_after=timedelta(seconds=timeouts.budget_gate_s // 2),
                    on_escalate=lambda: self.record_gate(
                        input.run_id, "intake", "escalated", timeouts
                    ),
                )
                self._phase = "running"
                await self.record_gate(input.run_id, "intake", outcome.kind, timeouts)
                if outcome.kind != "approved":
                    error = f"intake gate {outcome.kind}"
                    raise _GateClosed()
            result = await workflow.execute_activity(
                "dispatch_pm",
                args=[input.run_id, input.goal_text],
                start_to_close_timeout=timedelta(seconds=timeouts.dispatch_s),
                schedule_to_close_timeout=timedelta(
                    seconds=timeouts.dispatch_schedule_to_close_s
                ),
                heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
                retry_policy=DISPATCH_RETRY,
            )
        except _GateClosed:
            pass
        except asyncio.CancelledError:
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        self._phase = "finalizing"
        record = await workflow.execute_activity(
            "finalize_intake",
            args=[input.run_id, result, error],
            start_to_close_timeout=timedelta(seconds=timeouts.teardown_s),
            retry_policy=TEARDOWN_RETRY,
        )
        # The tickets this run filed are the standing heartbeat's next
        # backlog — nudge it; the reconcile timer covers a nudge that
        # goes missing.
        await self.wake_heartbeat(timeouts)
        return record


class _GateClosed(Exception):
    """Internal control flow: a rejected/expired gate skips the dispatch
    and falls through to finalization with its error."""
