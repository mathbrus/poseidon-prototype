"""The implement activities: durable, step-shaped.
Each is a thin client over the gateway, the
SandboxBackend, or agent HTTP; sequencing, timeouts, retries and
compensation live in the workflow, not here.

Retry safety, per activity:
    create_run        — upserts the run row; safe.
    fetch_workspace   — read-only; safe.
    provision_sandbox — the backend pre-removes a same-name remnant of a
                        crashed earlier attempt; registration upserts.
    implement_config  — read-only; safe.
    dispatch_executor — the agent answers 409 to a re-dispatch and the
                        activity falls through to polling; if the agent
                        lost the run (restart), the poll raises and the
                        retry re-dispatches from scratch.
    platform_verify   — read-only against the sandbox; safe.
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
from registry.bundles import AgentInstances, Companies
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
        instances: AgentInstances | None = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._runs = runs
        # Read-only: the applied blueprint's infra IR is the SandboxPolicy
        # source, and its workflow config directs the crew-only flow.
        self._companies = companies
        # Read-only: hire name -> standing instance (the conductor id the
        # crew request is recorded under).
        self._instances = instances
        # The identity revoke, called directly (same process) from teardown
        # so run-binding termination rides the one compensation anchor that
        # runs on every terminal path.
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

    @activity.defn(name="implement_config")
    async def implement_config(self) -> dict[str, Any]:
        """The workflow's shape, from the applied blueprint: which
        crew implements, which standing conductor's authority the crew
        request is recorded under, and how many verify-red re-dispatches
        the run gets. Fail-closed: no applied blueprint, no enabled
        implement workflow, no crew, or no standing conductor instance
        each refuse the run before anything is provisioned."""

        def read() -> dict[str, Any]:
            spec = self._companies.get(self._settings.company) or {}
            config = next(
                (w for w in spec.get("workflows") or [] if w.get("name") == "implement"),
                None,
            )
            if config is None or not config.get("enabled", False):
                raise RuntimeError(
                    f"company {self._settings.company!r} has no enabled"
                    " implement workflow in its applied blueprint"
                )
            crew_template = config.get("crew")
            if not crew_template:
                raise RuntimeError(
                    "implement workflow binds no crew — execution is"
                    " ephemeral crews; the compiler should have"
                    " refused this blueprint"
                )
            conductor_hire = config["conductor"]
            instance = None
            if self._instances is not None:
                instance = next(
                    (
                        i
                        for i in self._instances.for_company(self._settings.company)
                        if i.get("hire") == conductor_hire
                    ),
                    None,
                )
            if instance is None:
                raise RuntimeError(
                    f"no standing instance for conductor hire"
                    f" {conductor_hire!r} — is the company converged?"
                )
            return {
                "company": self._settings.company,
                "crew_template": crew_template,
                "conductor": instance["agent_id"],
                "conductor_role": instance["role_template"],
                "max_verify_retries": int(config.get("max_verify_retries") or 0),
            }

        return await asyncio.to_thread(read)

    def _sandbox_policy(self, run_id: str) -> SandboxPolicy:
        """The compiled SandboxPolicy: the applied blueprint's infra IR plus
        the per-run egress credential and the deploy-time resolution
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
    async def fetch_workspace(self, run_id: str, ref: str | None = None) -> bytes:
        """Archive the target repo at `ref` — the configured base ref by
        default; the review workflow passes the PR head ref instead
        (the review sandbox is a full sandbox from the PR ref)."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            workspace_tar = await gateway.fetch_workspace(
                run_id, self._settings.target_repo, ref or self._settings.base_ref
            )
        self._log.bind(run_id=run_id).info(
            "workspace fetched", extra={"bytes": len(workspace_tar)}
        )
        return workspace_tar

    @activity.defn(name="provision_sandbox")
    async def provision_sandbox(
        self, run_id: str, workspace_tar: bytes
    ) -> dict[str, Any]:
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

    @activity.defn(name="dispatch_executor")
    async def dispatch_executor(
        self, run_id: str, task_text: str, url: str, mode: str = "implement"
    ) -> dict[str, Any]:
        """Dispatch the ephemeral executor on its crew pod and poll until it
        reaches a terminal state, heartbeating every poll. The pod's own
        projected ServiceAccount token is the credential — no
        token rides this call or the workflow history. mode="implement"
        works to a completion claim; mode="deliver" is the separate,
        workflow-directed delivery dispatch, only
        ever issued after the platform verify gate is green."""
        async with AgentClient(url) as agent:
            await agent.dispatch(run_id, task_text, mode=mode)
            while True:
                _heartbeat()
                status = await agent.status(run_id)
                if status["status"] != "running":
                    self._log.bind(run_id=run_id).info(
                        "agent finished",
                        extra={"agent_status": status, "mode": mode},
                    )
                    return status
                await asyncio.sleep(self._settings.poll_interval_s)

    @activity.defn(name="platform_verify")
    async def platform_verify(self, run_id: str) -> dict[str, Any]:
        """The gate of record: the platform
        runs the sandbox's test suite under the orchestrator's own
        identity. The executor's claim is advisory; this result is what
        delivery hangs on."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.run_tests(run_id)
        passed = result.get("exit_code") == 0
        output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".strip()
        self._log.bind(run_id=run_id).info("platform verify", extra={"passed": passed})
        return {"passed": passed, "output": output[-4000:]}

    @activity.defn(name="teardown")
    async def teardown(
        self,
        run_id: str,
        handle: dict[str, Any] | None,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> dict[str, Any]:
        """Compensation + finalization: revoke the run's ephemeral
        identities (the binding rows die on every terminal
        path, before the token TTLs would), destroy the sandbox (when one
        was provisioned), retire its registration, and write the honest
        terminal run record, which is also the workflow's result."""
        log = self._log.bind(run_id=run_id)
        # Handle-independent on purpose: at 16d bind precedes provision, so
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
            try:
                gone = await self._crew.destroy_crew_pods(run_id)
                if gone:
                    log.info("crew pods destroyed", extra={"pods": gone})
            except Exception as exc:
                log.info("crew pod destroy failed", extra={"error": str(exc)})
                error = error or f"teardown: crew pod destroy failed: {exc}"
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
            self.implement_config,
            self.dispatch_executor,
            self.platform_verify,
            self.teardown,
        ]
