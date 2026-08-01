"""The orchestrator worker: connects to Temporal, registers the implement
workflow and its activities, runs until terminated.

    python -m orchestrator.worker

Deployed in the platform namespace. Credentials: the orchestrator bearer
token only (env) — no GIT_TOKEN, no provider keys. The Docker-socket
dependency of the transitional Docker sandbox backend disappears with the
k8s_jobs backend (plan step 4).
"""

import asyncio
import signal

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.worker import Worker

from common.telemetry import get_logger
from orchestrator.activities import ImplementActivities
from orchestrator.config import WorkerSettings
from orchestrator.workflows import ImplementWorkflow
from sandbox.backends.docker import DockerBackend


async def main() -> None:
    settings = WorkerSettings()
    log = get_logger("orchestrator.worker")
    backend = DockerBackend(
        image=settings.sandbox_image, network=settings.sandbox_network
    )
    activities = ImplementActivities(settings, backend)
    # On a fresh cluster the worker may come up before Temporal does (the
    # poseidon release installs first) — wait patiently instead of tripping
    # kubelet crash-loop backoff. The namespace check matters: pollers
    # started against a not-yet-registered namespace never recover.
    while True:
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=settings.temporal_namespace
            )
            await client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=settings.temporal_namespace)
            )
            break
        except Exception as exc:  # RPCError, DNS failures — anything transient
            log.info(
                "temporal not ready yet, retrying",
                extra={"address": settings.temporal_address, "error": str(exc)},
            )
            await asyncio.sleep(5)
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        loop.add_signal_handler(sig, stop.set)
    worker = Worker(
        client,
        task_queue=settings.task_queue,
        workflows=[ImplementWorkflow],
        activities=activities.all(),
    )
    log.info(
        "worker started",
        extra={"address": settings.temporal_address, "task_queue": settings.task_queue},
    )
    async with worker:
        await stop.wait()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
