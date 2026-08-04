"""The implement activities (§4.2): the five PoC flow steps, step-shaped as
before, now durable. Each is a thin client over the gateway, the
SandboxBackend, or agent HTTP; sequencing, timeouts, retries and
compensation live in the workflow, not here.

Retry safety, per activity:
    create_run        — upserts the run row; safe.
    fetch_workspace   — read-only; safe.
    provision_sandbox — the backend pre-removes a same-name remnant of a
                        crashed earlier attempt; registration upserts.
    dispatch_work     — the agent answers 409 to a re-dispatch and the
                        activity falls through to polling; if the agent
                        lost the run (restart), the poll raises and the
                        retry re-dispatches from scratch.
    teardown          — destroy is idempotent; the participants sweep and
                        the run row finalization are upserts.
"""

import asyncio
from typing import TYPE_CHECKING, Any

from temporalio import activity

if TYPE_CHECKING:
    from orchestrator.activities.crew import CrewActivities

from common.telemetry import get_logger
from compiler.infra import DEFAULT_IMAGE
from orchestrator.clients import AgentClient, GatewayClient
from orchestrator.config import WorkerSettings
from registry import Runs
from registry.bundles import Companies
from sandbox.contract import SandboxBackend, SandboxHandle, SandboxPolicy


def _heartbeat() -> None:
    # Activities stay directly callable in unit tests, outside any context.
    if activity.in_activity():
        activity.heartbeat()


class ImplementActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        backend: SandboxBackend,
        runs: Runs,
        companies: Companies,
        crew: "CrewActivities | None" = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._runs = runs
        # Read-only: the applied blueprint's infra IR is the SandboxPolicy
        # source (plan step 12, closing sequencing choice 2).
        self._companies = companies
        # The 16a revoke, called directly (same process) from teardown so
        # run-binding termination rides the one compensation anchor that
        # runs on every terminal path (plan step 16b).
        self._crew = crew
        self._log = get_logger("orchestrator.activities")

    @activity.defn(name="create_run")
    async def create_run(self, run_id: str, task_text: str) -> None:
        from orchestrator.workflows.implement import workflow_id

        await asyncio.to_thread(
            self._runs.start,
            run_id,
            task_text,
            "implement",
            workflow_id(run_id),
            self._settings.company,
        )
        self._log.bind(run_id=run_id).info("run created")

    def _sandbox_policy(self, run_id: str) -> SandboxPolicy:
        """The compiled SandboxPolicy: the applied blueprint's infra IR plus
        the per-run egress credential (§2b) and the deploy-time resolution
        of the logical toolchain image. No applied blueprint means no
        policy — provisioning without one would resurrect hand-authored
        values the compiler exists to replace."""
        spec = self._companies.get(self._settings.company)
        sandbox_ir = ((spec or {}).get("infra") or {}).get("sandbox")
        if not sandbox_ir:
            raise RuntimeError(
                f"no applied blueprint for company {self._settings.company!r}"
                " — provision_sandbox has no compiled SandboxPolicy source"
            )
        image = sandbox_ir.get("image") or DEFAULT_IMAGE
        if image == DEFAULT_IMAGE:
            image = self._settings.sandbox_image
        return SandboxPolicy(
            image=image,
            workspace_dir=sandbox_ir.get("workspace_dir", "/workspace"),
            cpu=sandbox_ir["cpu"],
            memory=sandbox_ir["memory"],
            disk=sandbox_ir["disk"],
            deadline_s=int(sandbox_ir["deadline_s"]),
            egress_allow=list(sandbox_ir.get("egress_allow") or []),
            env=self._settings.sandbox_proxy_env(run_id),
        )

    @activity.defn(name="fetch_workspace")
    async def fetch_workspace(self, run_id: str) -> bytes:
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            workspace_tar = await gateway.fetch_workspace(
                run_id, self._settings.target_repo, self._settings.base_ref
            )
        self._log.bind(run_id=run_id).info(
            "workspace fetched", extra={"bytes": len(workspace_tar)}
        )
        return workspace_tar

    @activity.defn(name="provision_sandbox")
    async def provision_sandbox(self, run_id: str, workspace_tar: bytes) -> dict[str, Any]:
        def provision():
            return self._backend.provision(
                run_id, workspace_tar, self._sandbox_policy(run_id)
            )

        handle = await asyncio.to_thread(provision)
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            await gateway.register_sandbox(
                run_id, handle.url, handle.namespace, handle.job_name
            )
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
        """Compensation + finalization: revoke the run's ephemeral
        identities (plan step 16b — the binding rows die on every terminal
        path, before the token TTLs would), destroy the sandbox (when one
        was provisioned), retire its registration, and write the honest
        terminal run record, which is also the workflow's result."""
        log = self._log.bind(run_id=run_id)
        # Handle-independent on purpose: at 16d mint precedes provision, so
        # participants can exist even when no sandbox ever did.
        if self._crew is not None:
            try:
                swept = await self._crew.revoke_participants(run_id)
                if swept:
                    log.info("participants revoked", extra={"agent_ids": swept})
            except Exception as exc:
                # A stale binding row is a governance problem, not a run
                # failure — but never a silent one.
                log.info("participant revoke failed", extra={"error": str(exc)})
                error = error or f"teardown: participant revoke failed: {exc}"
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
                async with GatewayClient(
                    self._settings.mcp_gateway_url,
                    self._settings.gateway_token(),
                ) as gateway:
                    await gateway.unregister_sandbox(run_id)
            except Exception as exc:
                # A stale registration is a governance problem, not a run
                # failure: the sandbox is already gone, so the address it
                # names answers nothing.
                log.info("sandbox unregister failed", extra={"error": str(exc)})
        result = result or {}
        error = error or result.get("error")
        record = await asyncio.to_thread(
            self._runs.finish,
            run_id,
            "failed" if error else "done",
            result.get("pr_url"),
            result.get("iterations", 0),
            error,
        )
        log.info("run finalized", extra={"status": record.status})
        return record.as_dict()

    def all(self) -> list[Any]:
        return [
            self.create_run,
            self.fetch_workspace,
            self.provision_sandbox,
            self.dispatch_work,
            self.teardown,
        ]
