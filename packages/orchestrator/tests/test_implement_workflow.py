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
from temporalio import activity
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from orchestrator.workflows.base import FAST_TIMEOUTS
from orchestrator.workflows.implement import ImplementInput, ImplementWorkflow

pytestmark = pytest.mark.anyio

RUN_ID = "run-implement-wf"
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

    dispatches: list[str] = field(default_factory=list)
    torn_down: list[tuple[dict[str, Any] | None, str | None]] = field(
        default_factory=list
    )
    attempts_recorded: list[str] = field(default_factory=list)
    wakes: int = 0

    def activities(self) -> list[Any]:
        harness = self

        @activity.defn(name="create_run")
        async def create_run(run_id: str, workflow: str, task: str) -> None: ...

        @activity.defn(name="check_budget")
        async def check_budget(run_id: str) -> dict[str, Any]:
            return {"cap_usd": 5.0, "spent_usd": 0.0}

        @activity.defn(name="workflow_config")
        async def workflow_config(workflow: str) -> dict[str, Any]:
            return {
                "company": "hello-saas",
                "conductor": {"agent_id": "pm-1", "role": "pm", "url": "http://pm:8000"},
                "crew": "feature-delivery",
                "gates": {},
                "max_verify_retries": 0,
                "budget_pause_minutes": 1,
                "contract_paths": [],
                "timeouts": asdict(FAST_TIMEOUTS),
            }

        @activity.defn(name="read_issue")
        async def read_issue(run_id: str, number: int) -> dict[str, Any]:
            return {
                "number": number,
                "title": "Add slugify",
                "body": "Deliver on `feature/slugify-x`.",
                "url": f"https://git.test/r/issues/{number}",
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

        @activity.defn(name="dispatch_agent")
        async def dispatch_agent(
            run_id: str, url: str, procedure: str, task_text: str
        ) -> dict[str, Any]:
            harness.dispatches.append(procedure)
            while harness.dispatch_hangs:
                # Heartbeat so the workflow's cancel reaches the activity.
                activity.heartbeat()
                await asyncio.sleep(0.05)
            return dict(harness.deliver if procedure == "deliver" else harness.implement)

        @activity.defn(name="platform_verify")
        async def platform_verify(run_id: str) -> dict[str, Any]:
            return {"passed": True, "output": ""}

        @activity.defn(name="record_attempts_exhausted")
        async def record_attempts_exhausted(run_id: str, workflow_id: str) -> dict:
            harness.attempts_recorded.append(run_id)
            return {"exhausted": False}

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
            dispatch_agent,
            platform_verify,
            record_attempts_exhausted,
            teardown,
            wake_heartbeat,
        ]


def anyio_backend() -> str:
    return "asyncio"


async def env():
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:
        pytest.skip(f"temporal test server unavailable: {exc}")
    yield environment
    await environment.shutdown()


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
    assert harness.attempts_recorded == []
    assert harness.wakes == 1  # the delivered PR is the heartbeat's to offer


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


async def test_a_delivery_that_opened_nothing_is_not_a_conclusion(env):
    harness = Harness(deliver={"status": "done", "error": None, "pr_url": None})
    record = await run_implement(env.client, harness)
    assert record["status"] == "failed"
    assert record["error"] == "delivery finished without a PR"
    assert harness.attempts_recorded == [RUN_ID]


async def test_a_cancelled_run_records_its_attempt(env):
    """A run cancelled while its implementer is dispatched opened no PR: the
    cancellation surfaces as the dispatch's, the run finalizes failed on
    it, and the attempt accounting still runs after teardown."""
    harness = Harness(dispatch_hangs=True)
    worker, task_queue = implement_worker(env.client, harness)
    await worker.__aenter__()
    try:
        handle = await env.client.start_workflow(
            "implement",
            ImplementInput(run_id=RUN_ID, issue_number=ISSUE),
            id=f"implement-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        for _ in range(200):
            if harness.dispatches:
                break
            await asyncio.sleep(0.05)
        assert harness.dispatches, "the implementer was never dispatched"
        await handle.cancel()
        record = await handle.result()
    finally:
        await worker.__aexit__(None, None, None)
    assert record["status"] == "failed"
    assert "cancelled" in record["error"].lower()
    assert harness.attempts_recorded == [RUN_ID]
