"""The console's Temporal client against a real Temporal: what a company
delete asks of it — end a loop, then delete every execution under its
id, each run of a continue-as-new chain included."""

import asyncio
import uuid

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from console.temporal import TemporalWorkflows, WorkflowNotFound

pytestmark = pytest.mark.anyio


@workflow.defn(name="chain", sandboxed=False)
class Chain:
    """A loop like the heartbeat: it continues as new `n` times, then
    waits for ever."""

    @workflow.run
    async def run(self, n: int) -> None:
        if n > 0:
            workflow.continue_as_new(n - 1)
        await workflow.wait_condition(lambda: False)


def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def temporal():
    # The dev server is fetched on first use; a machine that cannot fetch
    # it skips, like the orchestrator's workflow tests.
    try:
        environment = await WorkflowEnvironment.start_local()
    except Exception as exc:
        pytest.skip(f"temporal dev server unavailable: {exc}")
    yield environment
    await environment.shutdown()


async def executions(client: Client, wf_id: str) -> list[str]:
    return [e.run_id async for e in client.list_workflows(f"WorkflowId = '{wf_id}'")]


async def until(check, timeout_s: float = 90.0) -> None:
    """Temporal applies a delete asynchronously, and its listing follows:
    ask until it says so. The dev server takes about thirty seconds."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.2)


async def test_a_deleted_loop_leaves_no_execution(temporal):
    client = temporal.client
    workflows = TemporalWorkflows(client.service_client.config.target_host)
    wf_id = f"heartbeat-{uuid.uuid4().hex[:8]}"
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[Chain],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await client.start_workflow("chain", 2, id=wf_id, task_queue=task_queue)

        async def chained() -> bool:
            return len(await executions(client, wf_id)) == 3

        await until(chained)
        await workflows.terminate(wf_id, "company deleted")
        assert (await workflows.inspect(wf_id))["status"] == "TERMINATED"
        await workflows.delete(wf_id)

        async def gone() -> bool:
            return await executions(client, wf_id) == []

        await until(gone)
    with pytest.raises(WorkflowNotFound):
        await workflows.inspect(wf_id)
    with pytest.raises(WorkflowNotFound):
        await workflows.terminate(wf_id, "again")
    await workflows.delete(wf_id)  # deleting what is gone is nothing
