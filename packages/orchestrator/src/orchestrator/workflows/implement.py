"""The implement workflow (§4.2, §6.4 rows 1–3).

Activity sequencing with per-activity timeouts and retries (replacing the
PoC runner's `step_timeout_s` and polling loops) and guaranteed
compensation: teardown runs on success, failure, cancel, activity timeout,
and worker death (Temporal replays the workflow on the restarted worker and
the retry policies re-drive the interrupted activity).

Activities are referenced by name — the one seam the engine adapter needs:
this module imports nothing from the activity implementations.

M1 implement is gate-free by design (overview §9); the gates skeleton lives
in `orchestrator.gates` and is first wired at plan step 20.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

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
        except asyncio.CancelledError:
            # Cooperative cancel: compensate, then let the cancellation
            # close the workflow.
            cancelled = True
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        record = await workflow.execute_activity(
            "teardown",
            args=[input.run_id, handle, result, error],
            start_to_close_timeout=timedelta(seconds=timeouts.teardown_s),
            retry_policy=TEARDOWN_RETRY,
        )
        if cancelled:
            raise asyncio.CancelledError()
        return record
