"""The implement workflow's attempt accounting, proven at the workflow
layer: the real ImplementWorkflow runs against Temporal's time-skipping
test environment with scripted activities. What must hold: a run that
delivered concluded its issue and never asks; a run that ended without a
PR — a dispatch error, a delivery that opened nothing, a cancel — asks the
platform, after teardown, whether it spent the issue's last attempt."""

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import pytest
from conftest import state_is, until
from temporalio import activity
from temporalio.client import Client
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from orchestrator.workflows.base import FAST_TIMEOUTS
from orchestrator.workflows.implement import (
    ImplementInput,
    ImplementWorkflow,
    architecture_wait,
)

pytestmark = pytest.mark.anyio

RUN_ID = "run-implement-wf"
# The bounds the run's shape carries onto every dispatch.
BOUNDS = "Repository: github.com/example/repo, base ref main."
ISSUE = 5
PR_URL = "https://git.test/r/pulls/9"


@dataclass
class Harness:
    """Scripted activities + captured effects for one implement run."""

    implement: dict[str, Any] = field(
        default_factory=lambda: {"status": "done", "error": None, "iterations": 1}
    )
    deliver: dict[str, Any] = field(
        default_factory=lambda: {"status": "done", "error": None, "pr_url": PR_URL}
    )
    dispatch_hangs: bool = False
    # The ticket's refused deliveries, as `read_issue` reports them.
    refusals: list[dict[str, str]] = field(default_factory=list)

    dispatches: list[str] = field(default_factory=list)
    # (procedure, documents, bounds) per dispatch: the implement dispatches
    # carry the company's documents, delivery carries none, and every
    # dispatch carries the bounds the run's shape rendered.
    carried: list[tuple[str, dict[str, str], str]] = field(default_factory=list)
    torn_down: list[tuple[dict[str, Any] | None, str | None]] = field(
        default_factory=list
    )
    attempts_recorded: list[str] = field(default_factory=list)
    gate_events: list[str] = field(default_factory=list)
    wakes: int = 0
    verifies: int = 0
    # Scripted verify outcomes, consumed in order; green once exhausted.
    verify_outcomes: list[dict[str, Any]] = field(default_factory=list)
    # Scripted implement dispatch results, consumed in order; `implement`
    # once exhausted.
    implement_outcomes: list[dict[str, Any]] = field(default_factory=list)
    # Scripted deliver dispatch results, consumed in order; `deliver` once
    # exhausted.
    deliver_outcomes: list[dict[str, Any]] = field(default_factory=list)
    # The text each implement dispatch was given, in order.
    implement_texts: list[str] = field(default_factory=list)
    max_verify_retries: int = 0

    def activities(self) -> list[Any]:
        harness = self

        @activity.defn(name="create_run")
        async def create_run(run_id: str, workflow: str, task: str) -> None: ...

        @activity.defn(name="check_budget")
        async def check_budget(run_id: str) -> dict[str, Any]:
            return {"cap_usd": 5.0, "spent_usd": 0.0}

        @activity.defn(name="workflow_config")
        async def workflow_config(
            workflow: str, crewless: bool = False
        ) -> dict[str, Any]:
            return {
                "company": "hello-saas",
                "conductor": {"agent_id": "pm-1", "role": "pm", "url": "http://pm:8000"},
                "crew": "feature-delivery",
                "gates": {},
                "max_verify_retries": harness.max_verify_retries,
                "budget_deadline_minutes": 1,
                "contract_paths": [],
                "bounds": BOUNDS,
                "timeouts": asdict(FAST_TIMEOUTS),
            }

        @activity.defn(name="read_issue")
        async def read_issue(run_id: str, number: int) -> dict[str, Any]:
            return {
                "number": number,
                "title": "Add slugify",
                "body": "Deliver on `feature/slugify-x`.",
                "url": f"https://git.test/r/issues/{number}",
                "refusals": list(harness.refusals),
            }

        @activity.defn(name="fetch_workspace")
        async def fetch_workspace(run_id: str) -> str:
            return "workspace-tar"

        @activity.defn(name="provision_sandbox")
        async def provision_sandbox(run_id: str, tar: str) -> dict[str, Any]:
            return {"sandbox": "sbx-1"}

        @activity.defn(name="fulfill_crew")
        async def fulfill_crew(request: dict[str, Any]) -> dict[str, Any]:
            return {
                "agents": {
                    "implementer": {"agent_id": "fd-impl", "url": "http://crew:8000"}
                },
            }

        @activity.defn(name="dispatch_documents")
        async def dispatch_documents(run_id: str) -> dict[str, str]:
            return {"strategy": "Ship slugs."}

        @activity.defn(name="dispatch_agent")
        async def dispatch_agent(
            run_id: str,
            url: str,
            procedure: str,
            task_text: str,
            bounds: str,
            documents: dict[str, str],
        ) -> dict[str, Any]:
            harness.dispatches.append(procedure)
            harness.carried.append((procedure, documents, bounds))
            while harness.dispatch_hangs:
                # Heartbeat so the workflow's cancel reaches the activity.
                activity.heartbeat()
                await asyncio.sleep(0.05)
            if procedure == "deliver":
                if harness.deliver_outcomes:
                    return dict(harness.deliver_outcomes.pop(0))
                return dict(harness.deliver)
            harness.implement_texts.append(task_text)
            if harness.implement_outcomes:
                return dict(harness.implement_outcomes.pop(0))
            return dict(harness.implement)

        @activity.defn(name="platform_verify")
        async def platform_verify(run_id: str) -> dict[str, Any]:
            harness.verifies += 1
            if harness.verify_outcomes:
                return harness.verify_outcomes.pop(0)
            return {"passed": True, "output": ""}

        @activity.defn(name="abandon_exhausted")
        async def abandon_exhausted(run_id: str, workflow_id: str) -> dict:
            harness.attempts_recorded.append(run_id)
            return {"exhausted": False}

        @activity.defn(name="record_gate_outcome")
        async def record_gate_outcome(
            run_id: str, gate: str, kind: str, workflow_id: str
        ) -> None:
            harness.gate_events.append(kind)

        @activity.defn(name="teardown")
        async def teardown(
            run_id: str,
            handle: dict[str, Any] | None,
            result: dict[str, Any] | None,
            error: str | None,
        ) -> dict[str, Any]:
            harness.torn_down.append((result, error))
            error = error or (result or {}).get("error")
            return {
                "run_id": run_id,
                "status": "failed" if error else "done",
                "pr_url": (result or {}).get("pr_url"),
                "error": error,
            }

        @activity.defn(name="wake_heartbeat")
        async def wake_heartbeat() -> None:
            harness.wakes += 1

        return [
            create_run,
            check_budget,
            workflow_config,
            read_issue,
            fetch_workspace,
            provision_sandbox,
            fulfill_crew,
            dispatch_documents,
            dispatch_agent,
            platform_verify,
            abandon_exhausted,
            record_gate_outcome,
            teardown,
            wake_heartbeat,
        ]


def implement_worker(client: Client, harness: Harness) -> tuple[Worker, str]:
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    worker = Worker(
        client,
        task_queue=task_queue,
        workflows=[ImplementWorkflow],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    )
    return worker, task_queue


async def run_implement(client: Client, harness: Harness) -> dict[str, Any]:
    worker, task_queue = implement_worker(client, harness)
    async with worker:
        handle = await client.start_workflow(
            "implement",
            ImplementInput(run_id=RUN_ID, issue_number=ISSUE),
            id=f"implement-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        return await handle.result()


async def test_a_delivered_run_concluded_its_issue(env):
    harness = Harness()
    record = await run_implement(env.client, harness)
    assert record["status"] == "done" and record["pr_url"] == PR_URL
    assert harness.dispatches == ["implement", "deliver"]
    # The documents ride the implement dispatch as untrusted context and
    # never the delivery, which only ships what was verified; the bounds
    # ride both.
    assert harness.carried == [
        ("implement", {"strategy": "Ship slugs."}, BOUNDS),
        ("deliver", {}, BOUNDS),
    ]
    assert harness.attempts_recorded == []
    assert harness.wakes == 1  # the delivered PR is the heartbeat's to offer


async def test_a_retry_is_told_why_its_ticket_came_back(env):
    """The ticket's refused deliveries ride the implementer's task, after
    the body, so the next attempt reads what went wrong before."""
    harness = Harness(
        refusals=[{"pr_url": "https://git.test/r/pulls/5", "reason": "owner rejected it"}]
    )
    await run_implement(env.client, harness)
    assert harness.implement_texts[0].endswith(
        "Earlier deliveries of this ticket were not merged:\n"
        "- https://git.test/r/pulls/5: owner rejected it"
    )


async def test_an_implementer_that_died_leaves_the_issue_for_another_try(env):
    """A dispatch that came back with an error opened no PR: no conclusion.
    Teardown finalizes the run failed, then the attempt accounting runs —
    the issue is the heartbeat's to offer again, bounded by the cap."""
    harness = Harness(implement={"status": "failed", "error": "litellm.Timeout"})
    record = await run_implement(env.client, harness)
    assert record["status"] == "failed"
    assert harness.dispatches == ["implement"]
    assert harness.attempts_recorded == [RUN_ID]
    assert harness.wakes == 1  # a failed run frees the slot: still a nudge


async def test_an_architecture_request_ends_the_run_without_delivery(env):
    """The implementer asked for the plan to change instead of deviating:
    no verify, no delivery dispatch, the run fails by name with the ticket
    it waits on, the ticket rides the record, and the attempt accounting
    runs — a later attempt works the ticket against the revised plan."""
    url = "https://github.test/example/repo/issues/77"
    harness = Harness(
        implement={"status": "done", "error": None, "iterations": 1, "ticket_urls": [url]}
    )
    record = await run_implement(env.client, harness)
    assert record["status"] == "failed"
    assert harness.dispatches == ["implement"]
    assert harness.verifies == 0
    (result, error) = harness.torn_down[0]
    assert error == architecture_wait([url])
    assert result is not None and result["ticket_urls"] == [url]
    assert harness.attempts_recorded == [RUN_ID]
    assert harness.wakes == 1


async def test_a_delivery_that_opened_nothing_is_not_a_conclusion(env):
    harness = Harness(deliver={"status": "done", "error": None, "pr_url": None})
    record = await run_implement(env.client, harness)
    assert record["status"] == "failed"
    assert record["error"] == "delivery finished without a PR"
    assert harness.attempts_recorded == [RUN_ID]


REFUSAL = (
    "denied by policy rule: git_open_pr_architecture_path — docs/ARCHITECTURE.md"
    " is the architecture masterplan; only the steward delivers into it"
)
REFUSED = {
    "status": "failed",
    "error": "no PR",
    "pr_url": None,
    "delivery_refusal": REFUSAL,
}


async def test_a_refused_delivery_goes_back_to_implement_and_is_verified_again(env):
    """Deliver cannot change the work, so policy's refusal of a path is the
    implementer's to fix: it gets the platform's words, its fix is verified
    again, and only then delivered."""
    harness = Harness(deliver_outcomes=[REFUSED], max_verify_retries=1)
    record = await run_implement(env.client, harness)

    assert harness.dispatches == ["implement", "deliver", "implement", "deliver"]
    assert harness.verifies == 2
    assert REFUSAL in harness.implement_texts[1]
    assert record["status"] == "done"
    assert record["pr_url"] == PR_URL


async def test_refused_deliveries_share_the_verify_bound(env):
    """A red verify and a refused delivery each send the work back once
    against the same `max_verify_retries`; past it the run fails with the
    refusal on the record, and no PR."""
    harness = Harness(
        verify_outcomes=[{"passed": False, "output": "red"}],
        deliver_outcomes=[REFUSED],
        max_verify_retries=1,
    )
    record = await run_implement(env.client, harness)

    assert harness.dispatches == ["implement", "implement", "deliver"]
    assert record["status"] == "failed"
    assert record["error"].startswith(f"delivery refused: {REFUSAL} after 2 attempt(s)")
    assert harness.attempts_recorded == [RUN_ID]


@pytest.mark.parametrize(
    ("verdict", "outcomes"),
    [(False, ["parked", "rejected"]), (None, ["parked", "escalated", "expired"])],
)
async def test_a_budget_park_not_approved_ends_the_run_at_its_cap(env, verdict, outcomes):
    """A dispatch that hit the run's cap parks on `budget`. Rejected, or
    unanswered until the blueprint's deadline, it is never dispatched
    again: nothing is verified or delivered, the run finalizes failed on
    the cap, and the attempt accounting runs."""
    harness = Harness(
        implement_outcomes=[{"status": "failed", "error": "budget_exhausted: at cap"}]
    )
    worker, task_queue = implement_worker(env.client, harness)
    async with worker:
        handle = await env.client.start_workflow(
            "implement",
            ImplementInput(run_id=RUN_ID, issue_number=ISSUE),
            id=f"implement-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        # The park is reached in real time, its timers unfired until the
        # result is awaited.
        await until(
            lambda: state_is(handle, "parked-budget_exhausted"),
            "the run parked on its budget",
        )
        if verdict is not None:
            await handle.signal("budget", args=[verdict])
        record = await handle.result()

    assert harness.gate_events == outcomes
    assert harness.dispatches == ["implement"]
    assert harness.verifies == 0
    assert record["status"] == "failed"
    assert record["error"] == "budget_exhausted: at cap"
    assert harness.attempts_recorded == [RUN_ID]


async def test_a_cancelled_run_records_its_attempt(own_env):
    """A run cancelled while its implementer is dispatched opened no PR: the
    cancellation surfaces as the dispatch's, the run finalizes failed on
    it, and the attempt accounting still runs after teardown. The hung
    dispatch outlives the run on its server, hence a server of its own."""
    harness = Harness(dispatch_hangs=True)
    worker, task_queue = implement_worker(own_env.client, harness)
    await worker.__aenter__()
    try:
        handle = await own_env.client.start_workflow(
            "implement",
            ImplementInput(run_id=RUN_ID, issue_number=ISSUE),
            id=f"implement-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        await until(lambda: harness.dispatches, "the implementer was dispatched")
        await handle.cancel()
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)
    assert record["status"] == "failed"
    assert "cancelled" in record["error"].lower()
    assert harness.attempts_recorded == [RUN_ID]
