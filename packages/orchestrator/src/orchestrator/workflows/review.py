"""The review workflow (§6.3, plan step 20): PR in, human-gated merge out.

    create_review_run -> check_budget -> review_config -> read_pr ->
    fetch_workspace (PR head ref, O-4) -> provision_sandbox ->
    fulfill_crew (feature-review under the PM's conductor authority,
    §4.4 rule 3 — process-initiated is a first-class recorded request,
    not a bypass) -> dispatch the ephemeral reviewer (mode="review":
    reads the diff via git.read, runs the tests in its own sandbox,
    posts findings via git.comment) -> MERGE GATE -> approve: the
    orchestrator merges via git.merge_pr under its platform identity
    (O-1 A); reject/timeout: honest failure -> teardown.

The merge gate is the one human gate in the MVP, and the sole owner of the
merge guarantee (overview §2's named exception): the workflow parks on the
`merge` signal exactly when the blueprint configures `gates.merge` — gate
config comes from the compiled plan, never hardcoded (§9). Nobody home?
The timer escalates at half the deadline and expires the gate at the end
(§6.4); the park is API-visible (`state` query), the escalation and the
verdict are `process-gate` audit events written through an activity, and
the merge itself is a gateway-audited tool call — so one run_id query
tells the whole story: findings, park, escalation, verdict, merge.

Review is orchestrator-sequenced over HTTP; no SLIM anywhere on the
critical path (§6.3 [adopted]). Reject handling is the MVP's honest
minimum: findings are already on the PR, the run finalizes failed with
the verdict; the findings-loop/close refinement is deferred (§6.4).
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from orchestrator.gates import Gate
from orchestrator.workflows.implement import (
    BUDGET_MARKER,
    DISPATCH_RETRY,
    FAST_TIMEOUTS,
    STEP_RETRY,
    TEARDOWN_RETRY,
    Timeouts,
)


@dataclass
class ReviewInput:
    run_id: str
    pr_number: int
    timeouts: Timeouts | None = None


def review_workflow_id(run_id: str) -> str:
    """Run identity (§5.5): one workflow per run, deterministically named."""
    return f"review-{run_id}"


def review_task(pr_number: int, title: str, url: str) -> str:
    """The reviewer's dispatch text — composed by workflow code from the
    PR the platform read, so it is deterministic on replay."""
    return (
        f"Review pull request #{pr_number} ({url}): {title}\n\n"
        "Read the diff with the read_pr tool, run the test suite in your"
        " sandbox, and post your findings on the pull request as a single"
        " comment_pr call. Findings, not fixes: you review this change,"
        " you do not rewrite it."
    )


@workflow.defn(name="review")
class ReviewWorkflow:
    def __init__(self) -> None:
        self._phase = "running"
        self._budget_gate = Gate("budget")
        self._merge_gate = Gate("merge")

    @workflow.signal
    def budget(self, approved: bool, payload: str | None = None) -> None:
        """`poseidon signal <run_id> budget --workflow review`: resolve a
        budget pause, exactly as in implement."""
        self._budget_gate.resolve(approved, payload)

    @workflow.signal
    def merge(self, approved: bool, payload: str | None = None) -> None:
        """`poseidon signal <run_id> merge --approved/--no-approved
        --workflow review` — the human verdict the merge gate parks on."""
        self._merge_gate.resolve(approved, payload)

    @workflow.query
    def state(self) -> str:
        """The API-visible run phase: a parked merge gate must be a
        visible pending state (§6.4), never a silent hang."""
        return self._phase

    async def _dispatch(
        self,
        run_id: str,
        task_text: str,
        url: str,
        timeouts: Timeouts,
    ) -> dict[str, Any]:
        """The reviewer dispatch, budget-pause aware — the same loop as
        implement's: `budget_exhausted` parks the workflow on the `budget`
        signal, approval re-dispatches, anything else returns the marked
        result for teardown to finalize."""
        while True:
            result = await workflow.execute_activity(
                "dispatch_executor",
                args=[run_id, task_text, url, "review"],
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
                on_escalate=lambda: self._record_gate(
                    run_id, "budget", "escalated", timeouts
                ),
            )
            self._phase = "running"
            await self._record_gate(run_id, "budget", outcome.kind, timeouts)
            if outcome.kind != "approved":
                return result
            self._budget_gate = Gate("budget")

    async def _record_gate(
        self, run_id: str, gate: str, kind: str, timeouts: Timeouts
    ) -> None:
        await workflow.execute_activity(
            "record_gate_outcome",
            args=[run_id, gate, kind, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
            retry_policy=STEP_RETRY,
        )

    @workflow.run
    async def run(self, input: ReviewInput) -> dict[str, Any]:
        timeouts = input.timeouts or Timeouts()
        handle: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        error: str | None = None
        cancelled = False
        try:
            await workflow.execute_activity(
                "create_review_run",
                args=[input.run_id, input.pr_number],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            await workflow.execute_activity(
                "check_budget",
                args=[input.run_id],
                start_to_close_timeout=timedelta(seconds=timeouts.create_run_s),
                retry_policy=STEP_RETRY,
            )
            config = await workflow.execute_activity(
                "review_config",
                args=[],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            pr = await workflow.execute_activity(
                "read_pr",
                args=[input.run_id, input.pr_number],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            # O-4: a full sandbox from the PR ref — the reviewer runs
            # things, it does not just read diffs. Reuses the implement
            # provisioning verbatim.
            workspace_tar = await workflow.execute_activity(
                "fetch_workspace",
                args=[input.run_id, pr["head"]],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            handle = await workflow.execute_activity(
                "provision_sandbox",
                args=[input.run_id, workspace_tar],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            # §4.4 rule 3, process-initiated: the workflow records the
            # feature-review crew request under the PM's conductor
            # authority — same crew_requests row, same compiled may_spawn
            # decision an agent-issued request faces.
            crew = await workflow.execute_activity(
                "fulfill_crew",
                args=[{
                    "run_id": input.run_id,
                    "company": config["company"],
                    "crew_template": config["crew_template"],
                    "conductor": config["conductor"],
                    "conductor_role": config["conductor_role"],
                    "task_text": review_task(
                        input.pr_number, pr["title"], pr["url"]
                    ),
                    "record_under_authority": True,
                    "workflow": "review",
                }],
                start_to_close_timeout=timedelta(seconds=timeouts.fulfill_s),
                heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
                retry_policy=STEP_RETRY,
            )
            result = await self._dispatch(
                input.run_id, crew["brief"], crew["executor_url"], timeouts
            )
            if (result or {}).get("error"):
                raise _ReviewOver()
            if not (result or {}).get("comment_urls"):
                error = "reviewer finished without posting findings"
                raise _ReviewOver()
            # The run record links the PR under review whatever the gate
            # decides — the review happened either way.
            result = dict(result or {})
            result["pr_url"] = pr["url"]

            if config.get("merge_gate", "none") == "none":
                # The blueprint configured no human gate: the findings are
                # the deliverable and the workflow ends without merging —
                # an unattended auto-merge is not a default anyone gets
                # implicitly.
                raise _ReviewOver()

            # The merge gate (O-1 A): park on the signal, escalate at half
            # the deadline, expire at the end. Every transition is a
            # process-gate audit event written through an activity.
            self._phase = "paused-merge_gate"
            outcome = await self._merge_gate.wait(
                timeout=timedelta(seconds=timeouts.merge_gate_s),
                escalate_after=timedelta(seconds=timeouts.merge_gate_s // 2),
                on_escalate=lambda: self._record_gate(
                    input.run_id, "merge", "escalated", timeouts
                ),
            )
            self._phase = "running"
            await self._record_gate(input.run_id, "merge", outcome.kind, timeouts)
            if outcome.kind != "approved":
                error = f"merge gate {outcome.kind}"
                raise _ReviewOver()
            merged = await workflow.execute_activity(
                "merge_pr",
                args=[input.run_id, input.pr_number],
                start_to_close_timeout=timedelta(seconds=timeouts.step_s),
                retry_policy=STEP_RETRY,
            )
            result["merged"] = True
            result["merge_sha"] = merged.get("sha")
        except _ReviewOver:
            pass
        except asyncio.CancelledError:
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


class _ReviewOver(Exception):
    """Internal control flow: a terminal verdict (reviewer error, missing
    findings, gate reject/timeout, or a gate-free finish) skips straight
    to teardown with whatever result/error is in hand."""


__all__ = [
    "FAST_TIMEOUTS",
    "ReviewInput",
    "ReviewWorkflow",
    "review_task",
    "review_workflow_id",
]
