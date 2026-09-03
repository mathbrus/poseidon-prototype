"""The intake workflow: goal text in, tickets and
recorded crew intent out.

    create_run -> check_budget -> workflow_config -> [gate slot] ->
    dispatch the conductor's scope procedure -> teardown

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
signal before dispatching the PM. The dispatch rides `RunWorkflow.dispatch`,
so a budget stop parks on the `budget` signal exactly as in implement.
"""

import asyncio
from dataclasses import dataclass
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from orchestrator.gates import Gate
from orchestrator.workflows.base import RunWorkflow, gate_deadline


@dataclass
class IntakeInput:
    run_id: str
    goal_text: str


@workflow.defn(name="intake")
class IntakeWorkflow(RunWorkflow):
    WORKFLOW = "intake"

    def __init__(self) -> None:
        self._phase = "running"
        self._budget_gate = Gate("budget")
        self._intake_gate = Gate("intake")

    @workflow.signal
    def budget(self, approved: bool, payload: str | None = None) -> None:
        """`poseidon signal <run_id> budget --workflow intake`: resolve a
        budget pause, exactly as in implement."""
        self._budget_gate.resolve(approved, payload)

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
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            config = await self.start_run(input.run_id, input.goal_text)
            gate = config["gates"].get("intake") or {}
            if gate.get("resolved_by") == "human":
                # The configured gate: park until a human answers or the
                # declared deadline lapses. Transitions are process-gate
                # audit events like every other gate's.
                self._phase = "paused-intake_gate"
                outcome = await self._intake_gate.wait(
                    **gate_deadline(gate["deadline_minutes"]),
                    on_escalate=lambda: self.record_gate(
                        input.run_id, "intake", "escalated"
                    ),
                )
                self._phase = "running"
                await self.record_gate(input.run_id, "intake", outcome.kind)
                if outcome.kind != "approved":
                    error = f"intake gate {outcome.kind}"
                    raise _GateClosed()
            result = await self.dispatch(
                input.run_id,
                config["conductor"]["url"],
                "scope",
                input.goal_text,
            )
        except _GateClosed:
            pass
        except asyncio.CancelledError:
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        self._phase = "finalizing"
        record = await self.teardown(input.run_id, None, result, error)
        # The tickets this run filed are the standing heartbeat's next
        # backlog — nudge it; the reconcile timer covers a nudge that
        # goes missing.
        await self.wake_heartbeat()
        return record


class _GateClosed(Exception):
    """Internal control flow: a rejected/expired gate skips the dispatch
    and falls through to finalization with its error."""
