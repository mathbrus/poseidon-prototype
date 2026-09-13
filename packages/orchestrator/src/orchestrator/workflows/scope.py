"""The scope workflow: the conductor reads the observation and files the
next slice of work as tickets.

    create_run -> check_budget -> workflow_config -> scope_observation ->
    dispatch the conductor's scope procedure -> teardown

Started by the heartbeat like every run; no human hands it a text. The
observation is platform-composed — the company's documents and the world
as the tick sees it — and the PM does the scoping itself, through
governed tools: it files tickets via `issues.*`, each ending in the
`Crew:` trailer the implement run routes by. No crew is fulfilled: the
conductor is dispatched standing, in its own pod, and the workflow never
touches GitHub directly.

Scoping is the injection surface for the standing conductor: the
documents in the observation come from the repository and are untrusted.
Containment is the compiled policy's (the PM cannot execute, deliver, or
fulfil a crew outside its may_spawn list).
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from common.tasks import SCOPE_TASK
from orchestrator.gates import Gate
from orchestrator.workflows.base import STEP_RETRY, RunWorkflow


@dataclass
class ScopeInput:
    run_id: str


@workflow.defn(name="scope")
class ScopeWorkflow(RunWorkflow):
    WORKFLOW = "scope"
    CREWLESS = True

    def __init__(self) -> None:
        self._state = "running"
        self._budget_gate = Gate("budget")

    @workflow.signal
    def budget(self, approved: bool) -> None:
        """`poseidon signal <run_id> budget --workflow scope`: resolve a
        budget pause, exactly as in implement."""
        self._budget_gate.resolve(approved)

    @workflow.query
    def state(self) -> str:
        return self._state

    @workflow.run
    async def run(self, input: ScopeInput) -> dict[str, Any]:
        result: dict[str, Any] | None = None
        error: str | None = None
        try:
            config = await self.start_run(input.run_id, SCOPE_TASK)
            observation = await workflow.execute_activity(
                "scope_observation",
                args=[input.run_id],
                start_to_close_timeout=timedelta(seconds=self._bounds.step_s),
                retry_policy=STEP_RETRY,
            )
            result = await self.dispatch(
                input.run_id, config["conductor"]["url"], "scope", observation
            )
        except asyncio.CancelledError:
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        self._state = "finalizing"
        record = await self.teardown(input.run_id, None, result, error)
        # The tickets this run filed are the standing heartbeat's next
        # backlog — nudge it; the reconcile timer covers a nudge that
        # goes missing.
        await self.wake_heartbeat()
        return record
