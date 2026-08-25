"""What the workflows share: the per-activity bounds a run carries, the
retry policies, deterministic workflow naming, and — for the workflow
classes — the budget pause and the gate audit trail.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy

from orchestrator.gates import Gate

# The LLM gateway's hard-stop marker: a dispatch whose error carries it
# ran out of budget, and the workflow parks instead of retrying.
BUDGET_MARKER = "budget_exhausted"

STEP_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2.0)
# The agent works for as long as one attempt allows; heartbeats (one per
# poll) detect a dead worker fast, and the unbounded retry re-attaches to
# the running agent (dispatch answers 409 → poll). schedule_to_close caps
# the whole thing.
DISPATCH_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=0)
TEARDOWN_RETRY = RetryPolicy(maximum_attempts=5, backoff_coefficient=2.0)

FAMILIES = ("implement", "intake", "review", "heartbeat")


def workflow_id(family: str, key: str) -> str:
    """Deterministic workflow naming. The run-shaped families run one
    workflow per run (`implement-{run_id}`); the heartbeat is one
    standing workflow per company (`heartbeat-{company}`), keyed by company
    because the loop is standing structure and its audit story correlates
    on that id."""
    return f"{family}-{key}"


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
    # fulfill_crew hosts the planner's whole LLM loop, so it is
    # dispatch-shaped, not step-shaped.
    fulfill_s: int = 3600
    verify_s: int = 900
    teardown_s: int = 300
    # How long a budget pause waits for the `budget` signal before giving
    # up (escalation fires at half of it).
    budget_gate_s: int = 3600
    # How long the review workflow's merge gate waits for a human verdict
    # (escalation fires at half of it). Humans deserve a working day,
    # not an activity-sized bound — this is a durable workflow timer.
    merge_gate_s: int = 86400


# Sized for the deterministic mock brain (packages/mockllm): every bound is
# far above what the mock needs, and far below the patience a paid brain
# deserves — so a wedged activity surfaces in seconds, not minutes.
FAST_TIMEOUTS = Timeouts(
    create_run_s=15,
    step_s=60,
    dispatch_s=300,
    dispatch_schedule_to_close_s=600,
    dispatch_heartbeat_s=30,
    fulfill_s=300,
    verify_s=120,
    teardown_s=60,
    budget_gate_s=90,
    # Long enough for the e2e to watch the park, see the half-way
    # escalation land in the audit store, and still approve in time.
    merge_gate_s=60,
)


class RunWorkflow:
    """The base of the workflow classes: the API-visible phase, gate
    transitions recorded as `process-gate` audit events, and the
    budget-pause loop around an agent dispatch.

    Subclasses set `_phase` and `_budget_gate` and declare their own
    signals and the `state` query — Temporal reads those off the concrete
    class.
    """

    _phase: str
    _budget_gate: Gate

    async def record_gate(
        self, run_id: str, gate: str, kind: str, timeouts: Timeouts
    ) -> None:
        """A gate transition as a first-class `process-gate` audit event —
        written by an activity, since workflow code cannot touch the
        registry."""
        await workflow.execute_activity(
            "record_gate_outcome",
            args=[run_id, gate, kind, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
            retry_policy=STEP_RETRY,
        )

    async def dispatch(
        self, run_id: str, task_text: str, url: str, mode: str, timeouts: Timeouts
    ) -> dict[str, Any]:
        """One agent dispatch, budget-pause aware: a `budget_exhausted`
        result parks the workflow on the `budget` signal in an API-visible
        state; approval (the cap was raised) re-dispatches the same text,
        anything else returns the budget-marked result for teardown to
        finalize. Escalation and verdict are process-gate audit events,
        like the merge gate's — one Gate spine, one audit contract."""
        while True:
            result = await workflow.execute_activity(
                "dispatch_executor",
                args=[run_id, task_text, url, mode],
                start_to_close_timeout=timedelta(seconds=timeouts.dispatch_s),
                schedule_to_close_timeout=timedelta(
                    seconds=timeouts.dispatch_schedule_to_close_s
                ),
                heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
                retry_policy=DISPATCH_RETRY,
            )
            if BUDGET_MARKER not in ((result or {}).get("error") or ""):
                return result
            self._phase = f"paused-{BUDGET_MARKER}"
            outcome = await self._budget_gate.wait(
                timeout=timedelta(seconds=timeouts.budget_gate_s),
                escalate_after=timedelta(seconds=timeouts.budget_gate_s // 2),
                on_escalate=lambda: self.record_gate(
                    run_id, "budget", "escalated", timeouts
                ),
            )
            self._phase = "running"
            await self.record_gate(run_id, "budget", outcome.kind, timeouts)
            if outcome.kind != "approved":
                return result
            # Approved: a fresh gate for any next stop, then re-dispatch.
            self._budget_gate = Gate("budget")
