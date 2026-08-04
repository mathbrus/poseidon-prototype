"""The implement workflow (§4.2, §6.4 rows 1–3).

Activity sequencing with per-activity timeouts and retries (replacing the
PoC runner's `step_timeout_s` and polling loops) and guaranteed
compensation: teardown runs on success, failure, cancel, activity timeout,
and worker death (Temporal replays the workflow on the restarted worker and
the retry policies re-drive the interrupted activity).

Activities are referenced by name — the one seam the engine adapter needs:
this module imports nothing from the activity implementations.

M1 implement is gate-free by design (overview §9). The first wired gate is
the budget pause (plan step 14, §2b): a dispatch that comes back with the
LLM gateway's `budget_exhausted` marker parks the workflow on the `budget`
signal in an API-visible `paused-budget_exhausted` state — approve to
re-dispatch (the cap was raised), reject or time out to finalize. Process
gates from blueprint config land at plan step 20 on the same skeleton.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from orchestrator.gates import Gate

BUDGET_MARKER = "budget_exhausted"

STEP_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2.0)
# The agent works for as long as one attempt allows; heartbeats (one per
# poll) detect a dead worker fast, and the unbounded retry re-attaches to
# the running agent (dispatch answers 409 → poll). schedule_to_close caps
# the whole thing.
DISPATCH_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=0)
TEARDOWN_RETRY = RetryPolicy(maximum_attempts=5, backoff_coefficient=2.0)


@dataclass
class Timeouts:
    """Per-activity bounds, carried in the workflow input.

    Steps are bounded per activity, not per workflow: a workflow-level
    execution timeout would hard-kill without running compensation.

    They ride the input rather than module constants because the sizing
    depends on the *brain* behind the run, which only the client knows: a
    real LLM legitimately takes minutes per step, while the deterministic
    mock answers in milliseconds — and against the mock those generous
    bounds turn a fail-fast bug into a quarter hour of waiting on retries.
    Workflow code cannot read the environment (Temporal's sandbox), and
    input is deterministic on replay, so this is the seam.
    """

    create_run_s: int = 30
    step_s: int = 300
    dispatch_s: int = 3600
    dispatch_schedule_to_close_s: int = 7200
    dispatch_heartbeat_s: int = 60
    teardown_s: int = 300
    # How long a budget pause waits for the `budget` signal before giving
    # up (escalation fires at half of it).
    budget_gate_s: int = 3600


# Sized for the deterministic mock brain (packages/mockllm): every bound is
# far above what the mock needs, and far below the patience a paid brain
# deserves — so a wedged activity surfaces in seconds, not minutes.
FAST_TIMEOUTS = Timeouts(
    create_run_s=15,
    step_s=60,
    dispatch_s=300,
    dispatch_schedule_to_close_s=600,
    dispatch_heartbeat_s=30,
    teardown_s=60,
    budget_gate_s=90,
)


@dataclass
class ImplementInput:
    run_id: str
    task_text: str
    # None keeps the generous real-brain defaults (and old histories replay).
    timeouts: Timeouts | None = None


def workflow_id(run_id: str) -> str:
    """Run identity (§5.5): one workflow per run, deterministically named."""
    return f"implement-{run_id}"


@workflow.defn(name="implement")
class ImplementWorkflow:
    def __init__(self) -> None:
        self._phase = "running"
        self._budget_gate = Gate("budget")

    @workflow.signal
    def budget(self, approved: bool, payload: str | None = None) -> None:
        """`poseidon signal <run_id> budget --approved/--no-approved`:
        resolve a budget pause — approved means the cap was raised and the
        run may continue; rejected finalizes it."""
        self._budget_gate.resolve(approved, payload)

    @workflow.query
    def state(self) -> str:
        """The API-visible run phase (§4.8: a budget stop must be a visible
        pending state, never silent degradation)."""
        return self._phase

    @workflow.run
    async def run(self, input: ImplementInput) -> dict[str, Any]:
        timeouts = input.timeouts or Timeouts()
        handle: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        error: str | None = None
        cancelled = False
        try:
            await workflow.execute_activity(
                "create_run",
                args=[input.run_id, input.task_text],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            workspace_tar = await workflow.execute_activity(
                "fetch_workspace",
                args=[input.run_id],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            handle = await workflow.execute_activity(
                "provision_sandbox",
                args=[input.run_id, workspace_tar],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            while True:
                result = await workflow.execute_activity(
                    "dispatch_work",
                    args=[input.run_id, input.task_text],
                    start_to_close_timeout=timedelta(seconds=timeouts.dispatch_s),
                    schedule_to_close_timeout=timedelta(
                        seconds=timeouts.dispatch_schedule_to_close_s
                    ),
                    heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
                    retry_policy=DISPATCH_RETRY,
                )
                if BUDGET_MARKER not in ((result or {}).get("error") or ""):
                    break
                # The LLM gateway hard-stopped the run's spend (§2b): pause
                # into an API-visible state and wait for a human verdict —
                # never silently retry against an exhausted cap.
                self._phase = f"paused-{BUDGET_MARKER}"
                outcome = await self._budget_gate.wait(
                    timeout=timedelta(seconds=timeouts.budget_gate_s),
                    escalate_after=timedelta(seconds=timeouts.budget_gate_s // 2),
                )
                self._phase = "running"
                if outcome.kind != "approved":
                    # The dispatch result already carries the budget error;
                    # teardown finalizes the run with it.
                    break
                # Approved: the cap was raised — a fresh gate for any next
                # stop, then re-dispatch.
                self._budget_gate = Gate("budget")
                result = None
        except asyncio.CancelledError:
            # Cooperative cancel: compensate, then let the cancellation
            # close the workflow.
            cancelled = True
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        self._phase = "finalizing"
        record = await workflow.execute_activity(
            "teardown",
            args=[input.run_id, handle, result, error],
            start_to_close_timeout=timedelta(seconds=timeouts.teardown_s),
            retry_policy=TEARDOWN_RETRY,
        )
        if cancelled:
            raise asyncio.CancelledError()
        return record
