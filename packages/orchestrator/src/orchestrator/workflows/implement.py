"""The implement workflow.

Activity sequencing with per-activity timeouts and retries, and guaranteed
compensation: teardown runs on success, failure, cancel, activity timeout,
and worker death (Temporal replays the workflow on the restarted worker and
the retry policies re-drive the interrupted activity).

Activities are referenced by name — the one seam the engine adapter needs:
this module imports nothing from the activity implementations.

The crew is the only execution path: fulfill_crew stands the ephemeral
implementer up under the standing conductor's authority (the compiled
crew.request decision, evaluated as the conductor), the implementer is
dispatched at its
ephemeral pod with the task text, and its completion claim is advisory — the
platform verify gate (`platform_verify`, the orchestrator's own run of the
sandbox tests) is the gate of record. Red verify feeds the failure back for
a bounded number of re-dispatches (the blueprint's `max_verify_retries`);
only a green verify releases the separate `deliver` dispatch that opens
the PR. Delivery before verify is structurally impossible: the implement
procedure's toolset has no `git.open_pr`, and the deliver dispatch is
sequenced strictly after the gate.

The budget park (`RunWorkflow.dispatch`) rides the same Gate skeleton as
the review workflow's merge gate: it parks the workflow on the `budget`
signal in an API-visible `parked-budget_exhausted` state whenever a
dispatch comes back with the LLM gateway's `budget_exhausted` marker.
"""

import asyncio
from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.exceptions import ActivityError

from common.tasks import issue_run_task, ticket_crew
from common.workflows import BUDGET_GATE
from orchestrator.gates import Gate
from orchestrator.workflows.base import (
    STEP_RETRY,
    RunWorkflow,
)


@dataclass
class ImplementInput:
    """The ticket the company filed, resolved by the platform before
    dispatch — the one source of an implement run's text."""

    run_id: str
    issue_number: int


def issue_task(number: int, title: str, body: str, url: str) -> str:
    """The dispatch text of a run, composed by workflow code from the
    ticket the platform read — deterministic on replay. The body is
    untrusted input (bodies can run to 65k chars; truncation is future
    work, never done silently)."""
    return f"Implement issue #{number} ({url}): {title}\n\n{body}"


def architecture_wait(ticket_urls: list[str]) -> str:
    """The error an implement run ends with when its implementer asked for
    the plan to change instead of delivering: the stewardship ticket(s)
    the platform filed, by URL, so the run row says what it waits on."""
    return "waits on architecture request: " + ", ".join(ticket_urls)


def verify_feedback(task: str, output: str) -> str:
    """The red-path re-dispatch text: the task the implementer already worked
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
    WORKFLOW = "implement"

    def __init__(self) -> None:
        self._state = "running"
        self._budget_gate = Gate(BUDGET_GATE)

    @workflow.signal
    def budget(self, approved: bool) -> None:
        """`poseidon signal <run_id> budget --approved/--no-approved`:
        resolve a budget park — approved means the cap was raised and the
        run may continue; rejected finalizes it."""
        self._budget_gate.resolve(approved)

    @workflow.query
    def state(self) -> str:
        """The API-visible run phase (a budget stop must be a visible
        pending state, never silent degradation)."""
        return self._state

    @workflow.run
    async def run(self, input: ImplementInput) -> dict[str, Any]:
        handle: dict[str, Any] | None = None
        result: dict[str, Any] | None = None
        error: str | None = None
        cancelled = False
        try:
            config = await self.start_run(
                input.run_id, issue_run_task(input.issue_number)
            )
            # The one source of dispatch truth: the ticket the company
            # filed, fetched under the platform identity and composed
            # deterministically here.
            issue = await workflow.execute_activity(
                "read_issue",
                args=[input.run_id, input.issue_number],
                start_to_close_timeout=timedelta(seconds=self._bounds.step_s),
                retry_policy=STEP_RETRY,
            )
            task_source = issue_task(
                issue["number"], issue["title"], issue["body"], issue["url"]
            )
            workspace_tar = await workflow.execute_activity(
                "fetch_workspace",
                args=[input.run_id],
                start_to_close_timeout=timedelta(seconds=self._bounds.step_s),
                retry_policy=STEP_RETRY,
            )
            handle = await workflow.execute_activity(
                "provision_sandbox",
                args=[input.run_id, workspace_tar],
                start_to_close_timeout=timedelta(seconds=self._bounds.step_s),
                retry_policy=STEP_RETRY,
            )
            # The crew is the only execution path. The ticket names its
            # crew; the workflow's configured crew fulfills unnamed work.
            # Compiled policy bounds either through the same crew.request
            # decision at fulfill time.
            implementer = await self.fulfill(
                input.run_id, config, crew=ticket_crew(task_source)
            )

            # The company's documents ride the implement dispatches as
            # untrusted context — the base_ref copy, read by the platform,
            # never the sandbox's. Delivery reads none of them.
            documents = await self.dispatch_documents(input.run_id)
            # Implement to a completion claim; the platform verify gate is
            # the gate of record. Red feeds back, bounded by the blueprint.
            verify_failures = 0
            task_text = task_source
            verified = False
            # The tickets the implement dispatches filed, across every
            # verify retry: a re-dispatch replaces the result, never what
            # an earlier dispatch had the platform file.
            filed: list[str] = []
            while True:
                result = await self.dispatch(
                    input.run_id, implementer["url"], "implement", task_text, documents
                )
                filed += [
                    u for u in (result or {}).get("ticket_urls") or [] if u not in filed
                ]
                if result is not None:
                    result["ticket_urls"] = list(filed)
                if (result or {}).get("error"):
                    break
                if filed:
                    # The implementer asked for the plan to change rather
                    # than deviate from it: nothing here is delivered. The
                    # run fails by name, and the attempt accounting decides
                    # whether the ticket is offered again — a later attempt
                    # reads the plan the steward has revised by then.
                    error = architecture_wait(filed)
                    break
                verify = await workflow.execute_activity(
                    "platform_verify",
                    args=[input.run_id],
                    start_to_close_timeout=timedelta(seconds=self._bounds.verify_s),
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
                    input.run_id, implementer["url"], "deliver", task_source
                )
                if (delivery or {}).get("error"):
                    error = f"delivery failed: {delivery['error']}"
                elif not (delivery or {}).get("pr_url"):
                    error = "delivery finished without a PR"
                # The deliver status carries pr_url; keep the implement
                # iteration count alongside it, and every ticket either
                # dispatch filed — an architecture request is the
                # implement dispatch's, and delivery must not erase it.
                merged = dict(result or {})
                merged.update(
                    {k: v for k, v in (delivery or {}).items() if v is not None}
                )
                merged["ticket_urls"] = [
                    *((result or {}).get("ticket_urls") or []),
                    *((delivery or {}).get("ticket_urls") or []),
                ]
                result = merged
        except asyncio.CancelledError:
            # Cooperative cancel: compensate, then let the cancellation
            # close the workflow.
            cancelled = True
            error = "run cancelled"
        except ActivityError as exc:
            cause = exc.cause
            error = f"{exc.message}: {cause}" if cause else exc.message
        self._state = "finalizing"
        record = await self.teardown(input.run_id, handle, result, error)
        # Delivered is concluded, whatever happened after (a failed
        # teardown still delivered); a run that opened no PR aborted, and
        # the attempt accounting decides whether its issue stays offered.
        if not record.get("pr_url"):
            await self.record_attempts(input.run_id)
        # However this run ended, it changed the world the standing
        # heartbeat watches — nudge it; the reconcile timer covers a
        # nudge that goes missing.
        await self.wake_heartbeat()
        if cancelled:
            raise asyncio.CancelledError()
        return record
