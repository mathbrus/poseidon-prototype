"""Temporal smoke test (plan step 2 done-when): a worker connects to the
self-hosted Temporal on kind, runs a trivial workflow, and the workflow
history survives a Temporal pod restart — proving persistence really is in
Postgres, not in the pods.

The frontend is reached through `kubectl port-forward` (no NodePort: the
kind extraPortMappings are fixed at cluster creation, and nothing but this
test needs host access to Temporal yet).
"""

import asyncio
import contextlib
import socket
import subprocess
import time
import uuid
from datetime import timedelta

from temporalio import activity, workflow
from temporalio.client import Client, WorkflowExecutionStatus
from temporalio.worker import Worker

# The workflow sandbox re-imports this module per workflow run; anything
# non-deterministic (pytest, the conftest's httpx/runner imports) must be
# passed through rather than re-imported under restrictions.
with workflow.unsafe.imports_passed_through():
    import pytest

    from .conftest import PLATFORM_NS

LOCAL_PORT = 17233
SERVER_DEPLOYMENTS = [
    "temporal-frontend",
    "temporal-history",
    "temporal-matching",
    "temporal-worker",
]


@activity.defn
async def greet(name: str) -> str:
    return f"hello {name}"


@workflow.defn
class SmokeWorkflow:
    @workflow.run
    async def run(self, name: str) -> str:
        return await workflow.execute_activity(
            greet, name, start_to_close_timeout=timedelta(seconds=10)
        )


@contextlib.contextmanager
def frontend_port_forward():
    proc = subprocess.Popen(
        [
            "kubectl", "-n", PLATFORM_NS,
            "port-forward", "svc/temporal-frontend", f"{LOCAL_PORT}:7233",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail("kubectl port-forward exited early")
            try:
                with socket.create_connection(("127.0.0.1", LOCAL_PORT), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("port-forward to temporal-frontend never came up")
                time.sleep(0.5)
        yield f"127.0.0.1:{LOCAL_PORT}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def restart_temporal_server() -> None:
    for deploy in SERVER_DEPLOYMENTS:
        subprocess.run(
            ["kubectl", "-n", PLATFORM_NS, "rollout", "restart",
             f"deployment/{deploy}"],
            check=True, capture_output=True, timeout=60,
        )
    for deploy in SERVER_DEPLOYMENTS:
        subprocess.run(
            ["kubectl", "-n", PLATFORM_NS, "rollout", "status",
             f"deployment/{deploy}", "--timeout=300s"],
            check=True, capture_output=True, timeout=330,
        )


async def _run_smoke_workflow(address: str, workflow_id: str) -> None:
    client = await Client.connect(address)
    async with Worker(
        client,
        task_queue=f"queue-{workflow_id}",
        workflows=[SmokeWorkflow],
        activities=[greet],
    ):
        result = await client.execute_workflow(
            SmokeWorkflow.run,
            "poseidon",
            id=workflow_id,
            task_queue=f"queue-{workflow_id}",
            execution_timeout=timedelta(minutes=1),
        )
    assert result == "hello poseidon"


async def _assert_history_survived(address: str, workflow_id: str) -> None:
    client = await Client.connect(address)
    handle = client.get_workflow_handle(workflow_id)
    desc = await handle.describe()
    assert desc.status == WorkflowExecutionStatus.COMPLETED
    assert await handle.result() == "hello poseidon"


def test_workflow_survives_server_restart():
    workflow_id = f"smoke-{uuid.uuid4().hex[:8]}"

    # 1. Worker connects; a trivial workflow runs end-to-end.
    with frontend_port_forward() as address:
        asyncio.run(_run_smoke_workflow(address, workflow_id))

    # 2. Every Temporal server pod restarts; the port-forward dies with the
    # frontend pod, so a fresh one is opened after.
    restart_temporal_server()

    # 3. The completed workflow's history is still there, served by the new
    # pods out of Postgres.
    with frontend_port_forward() as address:
        asyncio.run(_assert_history_survived(address, workflow_id))
