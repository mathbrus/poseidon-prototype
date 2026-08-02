"""The implement activities (§4.2): the five PoC flow steps, step-shaped as
before, now durable. Each is a thin client over the gateway, the
SandboxBackend, or agent HTTP; sequencing, timeouts, retries and
compensation live in the workflow, not here.

Retry safety, per activity:
    create_run        — overwrites the manifest; safe.
    fetch_workspace   — read-only; safe.
    provision_sandbox — the backend pre-removes a same-name remnant of a
                        crashed earlier attempt; registration overwrites.
    dispatch_work     — the agent answers 409 to a re-dispatch and the
                        activity falls through to polling; if the agent
                        lost the run (restart), the poll raises and the
                        retry re-dispatches from scratch.
    teardown          — destroy is idempotent; the manifest write is a
                        plain overwrite.
"""

import asyncio
from typing import Any

from temporalio import activity

from common.telemetry import get_logger
from orchestrator.clients import AgentClient, GatewayClient
from orchestrator.config import WorkerSettings
from orchestrator.manifest import (
    Manifest,
    now,
    read_manifest,
    write_manifest,
)
from sandbox.contract import SandboxBackend, SandboxHandle


def _heartbeat() -> None:
    # Activities stay directly callable in unit tests, outside any context.
    if activity.in_activity():
        activity.heartbeat()


class ImplementActivities:
    def __init__(self, settings: WorkerSettings, backend: SandboxBackend) -> None:
        self._settings = settings
        self._backend = backend
        self._log = get_logger("orchestrator.activities")

    @activity.defn(name="create_run")
    async def create_run(self, run_id: str, task_text: str) -> None:
        write_manifest(
            self._settings.runs_dir,
            Manifest(run_id=run_id, task=task_text, status="running", created_at=now()),
        )
        self._log.bind(run_id=run_id).info("run created")

    @activity.defn(name="fetch_workspace")
    async def fetch_workspace(self, run_id: str) -> bytes:
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.orchestrator_bearer_token
        ) as gateway:
            workspace_tar = await gateway.fetch_workspace(
                self._settings.target_repo, self._settings.base_ref
            )
        self._log.bind(run_id=run_id).info(
            "workspace fetched", extra={"bytes": len(workspace_tar)}
        )
        return workspace_tar

    @activity.defn(name="provision_sandbox")
    async def provision_sandbox(self, run_id: str, workspace_tar: bytes) -> dict[str, Any]:
        handle = await asyncio.to_thread(
            self._backend.provision,
            run_id,
            workspace_tar,
            self._settings.sandbox_policy(run_id),
        )
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.orchestrator_bearer_token
        ) as gateway:
            await gateway.register_sandbox(run_id, handle.url)
        self._log.bind(run_id=run_id).info("sandbox ready", extra={"url": handle.url})
        return handle.model_dump()

    @activity.defn(name="dispatch_work")
    async def dispatch_work(self, run_id: str, task_text: str) -> dict[str, Any]:
        """Dispatch the agent and poll until it reaches a terminal state,
        heartbeating every poll. The PoC runner's `agent_timeout_s` poll
        loop becomes the activity's start-to-close + heartbeat timeouts."""
        async with AgentClient(self._settings.agent_url) as agent:
            await agent.dispatch(run_id, task_text)
            while True:
                _heartbeat()
                status = await agent.status(run_id)
                if status["status"] != "running":
                    self._log.bind(run_id=run_id).info(
                        "agent finished", extra={"agent_status": status}
                    )
                    return status
                await asyncio.sleep(self._settings.poll_interval_s)

    @activity.defn(name="teardown")
    async def teardown(
        self,
        run_id: str,
        handle: dict[str, Any] | None,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> dict[str, Any]:
        """Compensation + finalization: destroy the sandbox (when one was
        provisioned) and write the honest terminal run record, which is
        also the workflow's result."""
        log = self._log.bind(run_id=run_id)
        if handle is not None:
            try:
                await asyncio.to_thread(
                    self._backend.destroy, SandboxHandle.model_validate(handle)
                )
                log.info("sandbox destroyed")
            except Exception as exc:
                error = error or f"teardown: {exc}"
                log.info("teardown failed", extra={"error": str(exc)})
        try:
            manifest = read_manifest(self._settings.runs_dir, run_id)
        except FileNotFoundError:
            # create_run's write is gone (fresh volume?) — reconstruct.
            manifest = Manifest(
                run_id=run_id, task="", status="running", created_at=now()
            )
        result = result or {}
        manifest.pr_url = result.get("pr_url")
        manifest.iterations = result.get("iterations", 0)
        manifest.error = error or result.get("error")
        manifest.status = "failed" if manifest.error else "done"
        manifest.finished_at = now()
        write_manifest(self._settings.runs_dir, manifest)
        log.info("run finalized", extra={"status": manifest.status})
        return manifest.model_dump()

    def all(self) -> list[Any]:
        return [
            self.create_run,
            self.fetch_workspace,
            self.provision_sandbox,
            self.dispatch_work,
            self.teardown,
        ]
