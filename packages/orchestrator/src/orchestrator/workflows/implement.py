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
ephemeral pod with the task text, and its completion claim is advisory — the
platform verify gate (`platform_verify`, the orchestrator's own run of the
sandbox tests) is the gate of record. Red verify feeds the failure back for
a bounded number of re-dispatches (the blueprint's `max_verify_retries`);
only a green verify releases the separate `deliver` dispatch that opens
the PR. Delivery before verify is structurally impossible: the implement
procedure's toolset has no `git.open_pr`, and the deliver dispatch is
sequenced strictly after the gate.

The budget pause (`RunWorkflow.dispatch`) rides the same Gate skeleton as
the review workflow's merge gate: it parks the workflow on the `budget`
signal in an API-visible `paused-budget_exhausted` state whenever a
dispatch comes back with the LLM gateway's `budget_exhausted` marker.
"""

import asyncio
import re
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError, ApplicationError

from orchestrator.gates import Gate
from orchestrator.workflows.base import (
    STEP_RETRY,
    TEARDOWN_RETRY,
    RunWorkflow,
    Timeouts,
    crew_agent,
)


@dataclass
class ImplementInput:
    """Exactly one of `task_text` (a spec handed in directly) or
    `issue_number` (a GitHub issue the company already filed — resolved by
    the platform before dispatch) must be set."""

    run_id: str
    task_text: str | None = None
    # None keeps the generous real-brain defaults.
    timeouts: Timeouts | None = None
    issue_number: int | None = None


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


# A ticket names the crew that should fulfill it with a trailer line
# (`Crew: contract-authoring`); without one, the workflow's configured
# crew fulfills it — the perennial contract, not a fallback. The name
# grammar is the blueprint's `_NAME`.
TICKET_CREW_RE = re.compile(r"^Crew:\s*([a-z0-9][a-z0-9-]*)\s*$", re.MULTILINE)


def ticket_crew(task_source: str) -> str | None:
    """The crew the task names, parsed deterministically by workflow code.
    The *last* match wins: the trailer is written by whoever filed the
    ticket, and goal text quoted above it is untrusted input — a poisoned
    goal must not outrank the filer's own trailer. What the named crew can
    actually be is policy's to decide: fulfillment re-evaluates the
    compiled `crew.request` decision, so a ticket naming a template the
    blueprint never granted dies there, audited."""
    matches = TICKET_CREW_RE.findall(task_source)
    return matches[-1] if matches else None


def verify_feedback(task: str, output: str) -> str:
    """The red-path re-dispatch text: the task the executor already worked
    from, plus what the platform's own test run said. Deterministic —
    workflow code composes it on replay."""
    return (
        f"{task}\n\n"
        "The platform ran the test suite after your completion claim and it"
        " FAILED. Fix the failures; do not claim completion until the tests"
        " pass. Test output (tail):\n"
        f"{output}"
    )


@workflow.defn(name="implement")
class ImplementWorkflow(RunWorkflow):
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
            # ephemeral crew up — recording the crew request under the
            # conductor's authority via the same compiled decision the
            # crew.request tool faces — and hands back the executor's
            # identity and dispatch address.
            crew = await workflow.execute_activity(
                "fulfill_crew",
                args=[
                    {
                        "run_id": input.run_id,
                        "company": config["company"],
                        # The ticket names its crew; the workflow's
                        # configured crew fulfills unnamed work. Compiled
                        # policy bounds either through the same
                        # crew.request decision at fulfill time.
                        "crew_template": ticket_crew(task_source)
                        or config["crew_template"],
                        "conductor": config["conductor"],
                        "conductor_role": config["conductor_role"],
                        "record_under_authority": True,
                    }
                ],
                start_to_close_timeout=timedelta(seconds=timeouts.provision_s),
                heartbeat_timeout=timedelta(seconds=timeouts.dispatch_heartbeat_s),
                retry_policy=STEP_RETRY,
            )
            executor_url = crew_agent(crew)["url"]

            # Implement to a completion claim; the platform verify gate is
            # the gate of record. Red feeds back, bounded by the blueprint.
            verify_failures = 0
            task_text = task_source
            verified = False
            while True:
                result = await self.dispatch(
                    input.run_id, executor_url, "implement", task_text, timeouts
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
                task_text = verify_feedback(task_source, verify["output"])

            # Delivery is workflow-directed and gated: only a green
            # platform verify releases the deliver dispatch, which carries
            # the same task text the implementation worked from.
            if verified and not error and not (result or {}).get("error"):
                delivery = await self.dispatch(
                    input.run_id, executor_url, "deliver", task_source, timeouts
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
        # Delivered is concluded, whatever happened after (a failed
        # teardown still delivered); a run that opened no PR aborted, and
        # the attempt accounting decides whether its issue stays offered.
        if not record.get("pr_url"):
            await self.record_attempts(input.run_id, timeouts)
        # However this run ended, it changed the world the standing
        # heartbeat watches — nudge it; the reconcile timer covers a
        # nudge that goes missing.
        await self.wake_heartbeat(timeouts)
        if cancelled:
            raise asyncio.CancelledError()
        return record
