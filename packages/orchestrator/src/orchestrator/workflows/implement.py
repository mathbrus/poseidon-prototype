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

# Steps are bounded per activity, not per workflow: a workflow-level
# execution timeout would hard-kill without running compensation.
STEP_TIMEOUT = timedelta(seconds=300)
STEP_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2.0)
# The agent works for as long as one attempt allows; heartbeats (one per
# poll) detect a dead worker fast, and the unbounded retry re-attaches to
# the running agent (dispatch answers 409 → poll). schedule_to_close caps
# the whole thing.
DISPATCH_TIMEOUT = timedelta(hours=1)
DISPATCH_SCHEDULE_TO_CLOSE = timedelta(hours=2)
DISPATCH_HEARTBEAT = timedelta(seconds=60)
DISPATCH_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=0)
TEARDOWN_TIMEOUT = timedelta(seconds=300)
TEARDOWN_RETRY = RetryPolicy(maximum_attempts=5, backoff_coefficient=2.0)


@dataclass
class ImplementInput:
    run_id: str
    task_text: str


def workflow_id(run_id: str) -> str:
    """Run identity (§5.5): one workflow per run, deterministically named."""
    return f"implement-{run_id}"


@workflow.defn(name="implement")
class ImplementWorkflow:
    @workflow.run
    async def run(self, input: ImplementInput) -> dict[str, Any]:
        handle: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        error: str | None = None
        cancelled = False
        try:
            await workflow.execute_activity(
                "create_run",
                args=[input.run_id, input.task_text],
                start_to_close_timeout=timedelta(seconds=30),
                retry_policy=STEP_RETRY,
            )
            workspace_tar = await workflow.execute_activity(
                "fetch_workspace",
                args=[input.run_id],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=STEP_RETRY,
            )
            handle = await workflow.execute_activity(
                "provision_sandbox",
                args=[input.run_id, workspace_tar],
                start_to_close_timeout=STEP_TIMEOUT,
                retry_policy=STEP_RETRY,
            )
            result = await workflow.execute_activity(
                "dispatch_work",
                args=[input.run_id, input.task_text],
                start_to_close_timeout=DISPATCH_TIMEOUT,
                schedule_to_close_timeout=DISPATCH_SCHEDULE_TO_CLOSE,
                heartbeat_timeout=DISPATCH_HEARTBEAT,
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
            start_to_close_timeout=TEARDOWN_TIMEOUT,
            retry_policy=TEARDOWN_RETRY,
        )
        if cancelled:
            raise asyncio.CancelledError()
        return record
