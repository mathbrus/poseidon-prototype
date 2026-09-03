"""The orchestrator's own audit events: what the platform did on a run's
behalf, on the shared ingestion contract, so a run's story shows the
platform's actions beside the agents'."""

import asyncio

from common.audit import AuditEvent, Source
from common.identity import LIFECYCLE_PLATFORM, ORCHESTRATOR_ID, PLATFORM_ROLE
from registry import AuditStore


async def record_event(
    store: AuditStore,
    *,
    run_id: str,
    tool: str,
    digest: str,
    decision: str,
    rule: str,
    company: str,
    source: Source = "process-lifecycle",
    workflow: str = "",
    latency_ms: int = 0,
    backend_status: str | None = None,
    agent_id: str = ORCHESTRATOR_ID,
    role: str = PLATFORM_ROLE,
    lifecycle: str = LIFECYCLE_PLATFORM,
    parent_agent_id: str = "",
) -> AuditEvent:
    """Append one event under the platform identity by default — an
    ephemeral agent's own identity when the platform acts *for* it (its binding,
    its pod). The store is synchronous psycopg; it runs off the event
    loop."""
    event = AuditEvent(
        run_id=run_id,
        agent_id=agent_id,
        role=role,
        tool=tool,
        args_digest=digest,
        decision=decision,  # type: ignore[arg-type]
        rule=rule,
        latency_ms=latency_ms,
        backend_status=backend_status,  # type: ignore[arg-type]
        source=source,
        company=company,
        lifecycle=lifecycle,
        parent_agent_id=parent_agent_id,
        workflow=workflow,
    )
    await asyncio.to_thread(store.append, event)
    return event
