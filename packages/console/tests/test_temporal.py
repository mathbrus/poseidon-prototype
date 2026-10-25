"""The console's Temporal client against a real Temporal: what a company
delete asks of it — delete every execution the worker's task queue ran,
each run of a running continue-as-new chain included."""

import asyncio
import uuid

import pytest
from temporalio import workflow
from temporalio.client import Client
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from common.workflows import TASK_QUEUE
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


@workflow.defn(name="once", sandboxed=False)
class Once:
    """A run that ended long ago."""

    @workflow.run
    async def run(self) -> None:
        return None


@pytest.fixture
async def temporal():
    # The dev server is fetched on first use; a machine that cannot fetch
    # it skips, like the orchestrator's workflow tests. Its history service
    # acknowledges transfer tasks every thirty seconds by default, and a
    # delete waits on that acknowledgement: ask for one every second.
    try:
        environment = await WorkflowEnvironment.start_local(
            dev_server_extra_args=[
                "--dynamic-config-value",
                'history.transferProcessorUpdateAckInterval="1s"',
            ]
        )
    except Exception as exc:
        pytest.skip(f"temporal dev server unavailable: {exc}")
    yield environment
    await environment.shutdown()


async def listed(client: Client) -> list[str]:
    return [e.id async for e in client.list_workflows(f"TaskQueue = '{TASK_QUEUE}'")]


async def until(check, timeout_s: float = 20.0) -> None:
    """Temporal applies a delete asynchronously, and its listing follows:
    ask until it says so."""
    deadline = asyncio.get_running_loop().time() + timeout_s
    while not await check():
        assert asyncio.get_running_loop().time() < deadline, "timed out"
        await asyncio.sleep(0.2)


async def test_a_deleted_company_leaves_no_execution(temporal):
    client = temporal.client
    workflows = TemporalWorkflows(client.service_client.config.target_host)
    loop = f"heartbeat-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=TASK_QUEUE,
        workflows=[Chain, Once],
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        await client.execute_workflow("once", id="implement-run-1", task_queue=TASK_QUEUE)
        await client.start_workflow("chain", 2, id=loop, task_queue=TASK_QUEUE)

        async def chained() -> bool:
            return len(await listed(client)) == 4

        await until(chained)
        await workflows.delete_executions([loop])

        async def gone() -> bool:
            return await listed(client) == []

        await until(gone)
    for wf_id in (loop, "implement-run-1"):
        with pytest.raises(WorkflowNotFound):
            await workflows.inspect(wf_id)
    await workflows.delete_executions([loop])  # deleting what is gone is nothing
