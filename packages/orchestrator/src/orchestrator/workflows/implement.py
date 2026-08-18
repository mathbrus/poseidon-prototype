"""The implement workflow.

Activity sequencing with per-activity timeouts and retries, and guaranteed
compensation: teardown runs on success, failure, cancel, activity timeout,
and worker death (Temporal replays the workflow on the restarted worker and
the retry policies re-drive the interrupted activity).

Activities are referenced by name — the one seam the engine adapter needs:
this module imports nothing from the activity implementations.

The crew is the only execution path: fulfill_crew stands
the ephemeral executor up (recording the crew request under the standing
conductor's authority when none exists), the executor is dispatched at its
crew pod, and its completion claim is advisory — the platform verify gate
(`platform_verify`, the orchestrator's own run of the sandbox tests) is the
gate of record. Red verify feeds the failure back for a bounded number of
re-dispatches (the blueprint's `max_verify_retries`); only a green verify
releases the separate mode="deliver" dispatch that
opens the PR. Delivery before verify is structurally impossible: the
implement-mode toolset has no `git.open_pr`, and the deliver dispatch is
sequenced strictly after the gate.

The budget pause rides the same Gate skeleton as the review workflow's
merge gate: it parks the workflow on the `budget` signal in an API-visible
`paused-budget_exhausted` state whenever a dispatch comes back with the
LLM gateway's `budget_exhausted` marker.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError, ApplicationError

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


@dataclass
class ImplementInput:
    """Exactly one of `task_text` (a spec handed in directly) or
    `issue_number` (a GitHub issue the company already filed — resolved by
    the platform before dispatch) must be set."""

    run_id: str
    task_text: str | None = None
    # None keeps the generous real-brain defaults (and old histories replay).
    timeouts: Timeouts | None = None
    issue_number: int | None = None


def workflow_id(run_id: str) -> str:
    """One workflow per run, deterministically named."""
    return f"implement-{run_id}"


def issue_placeholder(number: int) -> str:
    """The run row's task for an issue-driven run: the row is created
    before the ticket is resolved (so it exists ahead of the platform's
    gateway call), and the reference — not a pasted body — is what the
    record keeps."""
    return f"implement issue #{number}"


def issue_task(number: int, title: str, body: str, url: str) -> str:
    """The dispatch text for an issue-driven run, composed by workflow code
    from what the platform read — deterministic on replay. The body is
    untrusted input, exactly like a task file's contents (bodies can run to
    65k chars; truncation is future work, never done silently)."""
    return f"Implement issue #{number} ({url}): {title}\n\n{body}"


def verify_feedback(brief: str, output: str) -> str:
    """The red-path re-dispatch text: the brief the executor already worked
    from, plus what the platform's own test run said. Deterministic —
    workflow code composes it on replay."""
    return (
        f"{brief}\n\n"
        "The platform ran the test suite after your completion claim and it"
        " FAILED. Fix the failures; do not claim completion until the tests"
        " pass. Test output (tail):\n"
        f"{output}"
    )


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
        """The API-visible run phase (a budget stop must be a visible
        pending state, never silent degradation)."""
        return self._phase

    async def _record_gate(
        self, run_id: str, gate: str, kind: str, timeouts: Timeouts
    ) -> None:
        """A gate transition as a first-class `process-gate` audit event:
        a budget stop escalates — visibly, in the audit
        store, not just in a workflow timer."""
        await workflow.execute_activity(
            "record_gate_outcome",
            args=[run_id, gate, kind, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
            retry_policy=STEP_RETRY,
        )

    async def _dispatch(
        self,
        run_id: str,
        task_text: str,
        url: str,
        mode: str,
        timeouts: Timeouts,
    ) -> dict[str, Any]:
        """One executor dispatch, budget-pause aware: a `budget_exhausted`
        result parks the workflow on the `budget` signal; approval (the cap
        was raised) re-dispatches the same text, anything else returns the
        budget-marked result for teardown to finalize."""
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
            # The LLM gateway hard-stopped the run's spend: pause
            # into an API-visible state and wait for a human verdict —
            # never silently retry against an exhausted cap. Escalation
            # and verdict are process-gate audit events, like the merge
            # gate's (one Gate spine, one audit contract).
            self._phase = f"paused-{BUDGET_MARKER}"
            outcome = await self._budget_gate.wait(
                timeout=timedelta(seconds=timeouts.budget_gate_s),
                escalate_after=timedelta(seconds=timeouts.budget_gate_s // 2),
                on_escalate=lambda: self._record_gate(
                    run_id, "budget", "escalated", timeouts
                ),
            )
            self._phase = "running"
            await self._record_gate(run_id, "budget", outcome.kind, timeouts)
            if outcome.kind != "approved":
                # The dispatch result already carries the budget error;
                # teardown finalizes the run with it.
                return result
            # Approved: a fresh gate for any next stop, then re-dispatch.
            self._budget_gate = Gate("budget")

    @workflow.run
    async def run(self, input: ImplementInput) -> dict[str, Any]:
        if (input.task_text is None) == (input.issue_number is None):
            raise ApplicationError(
                "exactly one of task_text or issue_number must be set",
                non_retryable=True,
            )
        timeouts = input.timeouts or Timeouts()
        handle: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        error: str | None = None
        cancelled = False
        try:
            await workflow.execute_activity(
                "create_run",
                args=[
                    input.run_id,
                    input.task_text or issue_placeholder(input.issue_number),
                ],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            # The run is born with headroom or not at all — the
            # same activity-boundary ledger observation intake makes
            # (enforcement itself stays local at the LLM gateway).
            await workflow.execute_activity(
                "check_budget",
                args=[input.run_id],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            config = await workflow.execute_activity(
                "implement_config",
                args=[],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            # The one source of dispatch truth: the file text handed in, or
            # the ticket the company already filed — fetched under the
            # platform identity and composed deterministically here.
            if input.issue_number is not None:
                issue = await workflow.execute_activity(
                    "read_issue",
                    args=[input.run_id, input.issue_number],
                    start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                    retry_policy=STEP_RETRY,
                )
                task_source = issue_task(
                    issue["number"], issue["title"], issue["body"], issue["url"]
                )
            else:
                task_source = input.task_text
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
            # The crew is the only execution path: fulfill stands the
            # ephemeral roster up — recording the crew request under the
            # conductor's authority via the same compiled decision the
            # crew.request tool faces — and hands back the planner's brief
            # plus the executor's crew-pod address.
            crew = await workflow.execute_activity(
                "fulfill_crew",
                args=[
                    {
                        "run_id": input.run_id,
                        "company": config["company"],
                        "crew_template": config["crew_template"],
                        "conductor": config["conductor"],
                        "conductor_role": config["conductor_role"],
                        "task_text": task_source,
                        "record_under_authority": True,
                    }
                ],
                start_to_close_timeout=timedelta(seconds=timeouts.fulfill_s),
                heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
                retry_policy=STEP_RETRY,
            )
            brief = crew["brief"]
            executor_url = crew["executor_url"]

            # Implement to a completion claim; the platform verify gate is
            # the gate of record. Red feeds back, bounded by the blueprint.
            verify_failures = 0
            task_text = brief
            verified = False
            while True:
                result = await self._dispatch(
                    input.run_id, task_text, executor_url, "implement", timeouts
                )
                if (result or {}).get("error"):
                    break
                verify = await workflow.execute_activity(
                    "platform_verify",
                    args=[input.run_id],
                    start_to_close_timeout=timedelta(seconds=timeouts.verify_s),
                    retry_policy=STEP_RETRY,
                )
                if verify["passed"]:
                    verified = True
                    break
                verify_failures += 1
                if verify_failures > config["max_verify_retries"]:
                    error = (
                        "platform verify failed after"
                        f" {verify_failures} attempt(s) — bounded by"
                        f" max_verify_retries={config['max_verify_retries']}"
                    )
                    break
                task_text = verify_feedback(brief, verify["output"])

            # Delivery is workflow-directed (choice 8) and gated: only a
            # green platform verify releases the deliver dispatch. The
            # source text rides it — not the planner's brief — so the
            # delivery constraints (branch, markers) survive verbatim.
            if verified and not error and not (result or {}).get("error"):
                delivery = await self._dispatch(
                    input.run_id, task_source, executor_url, "deliver", timeouts
                )
                if (delivery or {}).get("error"):
                    error = f"delivery failed: {delivery['error']}"
                elif not (delivery or {}).get("pr_url"):
                    error = "delivery finished without a PR"
                # The deliver status carries pr_url; keep the implement
                # iteration count alongside it.
                merged = dict(result or {})
                merged.update(
                    {k: v for k, v in (delivery or {}).items() if v is not None}
                )
                result = merged
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
