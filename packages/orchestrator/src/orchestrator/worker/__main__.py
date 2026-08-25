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
    IntakeActivities,
    ReviewActivities,
)
from orchestrator.config import WorkerSettings
from orchestrator.ephemeral_pods import EphemeralPodProvisioner
from orchestrator.workflows import (
    HeartbeatInput,
    HeartbeatWorkflow,
    ImplementWorkflow,
    IntakeWorkflow,
    ReviewWorkflow,
)
from orchestrator.workflows.base import workflow_id
from registry import (
    AgentInstances,
    AuditStore,
    BudgetLedger,
    Companies,
    CrewRequests,
    RunParticipants,
    Runs,
    open_registry,
)
from sandbox.k8s_jobs import K8sJobsBackend


async def ensure_heartbeat_loop(
    client: Client, settings: WorkerSettings, companies: Companies, log
) -> None:
    """Reconcile-on-goal: while the applied blueprint declares the
    section, the company's `heartbeat-{company}` workflow is running; start
    it if it is not. The other half of the loop is the workflow's own
    config read — it stops itself when the section is gone — so this side
    only ever starts, never kills."""
    while True:
        await ensure_heartbeat_once(client, settings, companies, log)
        await asyncio.sleep(settings.heartbeat_ensure_interval_s)


async def ensure_heartbeat_once(
    client: Client, settings: WorkerSettings, companies: Companies, log
) -> None:
    """One ensure pass, isolated so a hiccup never kills the loop."""
    try:
        spec = await asyncio.to_thread(companies.get, settings.company)
        if (spec or {}).get("heartbeat"):
            await client.start_workflow(
                "heartbeat",
                HeartbeatInput(company=settings.company),
                id=workflow_id("heartbeat", settings.company),
                task_queue=settings.task_queue,
            )
            log.info("heartbeat started", extra={"company": settings.company})
    except WorkflowAlreadyStartedError:
        pass  # the heartbeat is already beating — the normal case
    except Exception as exc:  # registry or Temporal hiccup: retry next pass
        log.info("heartbeat ensure pass failed, retrying", extra={"error": str(exc)})


async def main() -> None:
    settings = WorkerSettings()
    log = get_logger("orchestrator.worker")
    backend = K8sJobsBackend(namespace=settings.sandbox_namespace)
    # Every service migrates at startup under an advisory lock, so the
    # worker needs no ordering guarantee against the other writers.
    registry = open_registry()
    crew_activities = CrewActivities(
        settings,
        RunParticipants(registry),
        AuditStore(registry),
        CrewRequests(registry),
        Companies(registry),
        BudgetLedger(registry),
        Runs(registry),
        provisioner=EphemeralPodProvisioner(settings.sandbox_namespace),
    )
    activities = ImplementActivities(
        settings,
        backend,
        Runs(registry),
        Companies(registry),
        AgentInstances(registry),
        crew_activities,
    )
    intake_activities = IntakeActivities(
        settings, Runs(registry), Companies(registry), BudgetLedger(registry)
    )
    review_activities = ReviewActivities(
        settings,
        Runs(registry),
        Companies(registry),
        AgentInstances(registry),
        AuditStore(registry),
        RunParticipants(registry),
    )
    heartbeat_activities = HeartbeatActivities(
        settings,
        Runs(registry),
        Companies(registry),
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
        workflows=[HeartbeatWorkflow, ImplementWorkflow, IntakeWorkflow, ReviewWorkflow],
        activities=[
            *activities.all(),
            *crew_activities.all(),
            *intake_activities.all(),
            *review_activities.all(),
            *heartbeat_activities.all(),
        ],
    )
    log.info(
        "worker started",
        extra={"address": settings.temporal_address, "task_queue": settings.task_queue},
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
