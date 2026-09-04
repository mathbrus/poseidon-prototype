"""The orchestrator worker: connects to Temporal, registers the
workflows and their activities, runs until terminated.

    python -m orchestrator.worker

Deployed in the platform namespace. Credentials: its projected
service-account token (GATEWAY_TOKEN_FILE) and the egress-proxy HMAC key —
no GIT_TOKEN, no provider keys. Sandboxes are Kubernetes Jobs in the
company namespace; the worker's ServiceAccount carries the matching bounded
Role, its only Kubernetes-API surface.
"""

import asyncio
import signal

from temporalio.api.workflowservice.v1 import DescribeNamespaceRequest
from temporalio.client import Client
from temporalio.exceptions import WorkflowAlreadyStartedError
from temporalio.worker import Worker

from common.telemetry import get_logger
from orchestrator.activities import (
    CrewActivities,
    HeartbeatActivities,
    ImplementActivities,
    ReviewActivities,
    RunActivities,
)
from orchestrator.config import WorkerSettings
from orchestrator.ephemeral_pods import EphemeralPodProvisioner
from orchestrator.workflows import (
    HeartbeatInput,
    HeartbeatWorkflow,
    ImplementWorkflow,
    ReviewWorkflow,
    ScopeWorkflow,
)
from orchestrator.workflows.base import TASK_QUEUE, TEMPORAL_NAMESPACE, workflow_id
from registry import (
    AgentInstances,
    AuditStore,
    BudgetLedger,
    Companies,
    RunParticipants,
    Runs,
    open_registry,
)
from sandbox.k8s_jobs import K8sJobsBackend

# How often the worker checks that the company's heartbeat workflow is
# running (the applied blueprint is the desired state; the workflow itself
# stops when the company is gone).
HEARTBEAT_ENSURE_INTERVAL_S = 60


async def ensure_heartbeat_loop(
    client: Client, settings: WorkerSettings, companies: Companies, log
) -> None:
    """While the company is applied, its `heartbeat-{company}` workflow
    is running; start it if it is not. The other half of the loop is the
    workflow's own config read — it stops itself when the company is
    gone — so this side only ever starts, never kills."""
    while True:
        await ensure_heartbeat_once(client, settings, companies, log)
        await asyncio.sleep(HEARTBEAT_ENSURE_INTERVAL_S)


async def ensure_heartbeat_once(
    client: Client, settings: WorkerSettings, companies: Companies, log
) -> None:
    """One ensure pass, isolated so a hiccup never kills the loop."""
    try:
        if await asyncio.to_thread(companies.get, settings.company):
            await client.start_workflow(
                "heartbeat",
                HeartbeatInput(company=settings.company),
                id=workflow_id("heartbeat", settings.company),
                task_queue=TASK_QUEUE,
            )
            log.info("heartbeat started", extra={"company": settings.company})
    except WorkflowAlreadyStartedError:
        pass  # the heartbeat is already beating — the normal case
    except Exception as exc:  # registry or Temporal hiccup: retry next pass
        log.info("heartbeat ensure pass failed, retrying", extra={"error": str(exc)})


async def main() -> None:
    settings = WorkerSettings()
    log = get_logger("orchestrator.worker")
    backend = K8sJobsBackend(namespace=settings.company)
    # Every service migrates at startup under an advisory lock, so the
    # worker needs no ordering guarantee against the other writers.
    registry = open_registry()
    crew_activities = CrewActivities(
        settings,
        RunParticipants(registry),
        AuditStore(registry),
        Companies(registry),
        BudgetLedger(registry),
        Runs(registry),
        provisioner=EphemeralPodProvisioner(settings.company),
    )
    run_activities = RunActivities(
        settings,
        backend,
        Runs(registry),
        Companies(registry),
        AgentInstances(registry),
        BudgetLedger(registry),
        AuditStore(registry),
        crew_activities,
    )
    implement_activities = ImplementActivities(
        settings, backend, Runs(registry), Companies(registry)
    )
    review_activities = ReviewActivities(
        settings,
        Runs(registry),
        Companies(registry),
        AuditStore(registry),
        RunParticipants(registry),
    )
    heartbeat_activities = HeartbeatActivities(
        settings,
        Runs(registry),
        Companies(registry),
        AgentInstances(registry),
        BudgetLedger(registry),
        AuditStore(registry),
    )
    # On a fresh cluster the worker may come up before Temporal does (the
    # poseidon release installs first) — wait patiently instead of tripping
    # kubelet crash-loop backoff. The namespace check matters: pollers
    # started against a not-yet-registered namespace never recover.
    while True:
        try:
            client = await Client.connect(
                settings.temporal_address, namespace=TEMPORAL_NAMESPACE
            )
            await client.workflow_service.describe_namespace(
                DescribeNamespaceRequest(namespace=TEMPORAL_NAMESPACE)
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
        task_queue=TASK_QUEUE,
        workflows=[HeartbeatWorkflow, ImplementWorkflow, ReviewWorkflow, ScopeWorkflow],
        activities=[
            *run_activities.all(),
            *implement_activities.all(),
            *crew_activities.all(),
            *review_activities.all(),
            *heartbeat_activities.all(),
        ],
    )
    log.info(
        "worker started",
        extra={"address": settings.temporal_address, "task_queue": TASK_QUEUE},
    )
    ensure = asyncio.create_task(
        ensure_heartbeat_loop(client, settings, Companies(registry), log)
    )
    try:
        async with worker:
            await stop.wait()
    finally:
        ensure.cancel()
    log.info("worker stopped")


if __name__ == "__main__":
    asyncio.run(main())
