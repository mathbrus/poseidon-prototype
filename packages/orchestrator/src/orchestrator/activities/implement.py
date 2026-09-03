"""The implement activities: the work between a run's birth and its
teardown (which `RunActivities` owns for every workflow). Each is a thin
client over the gateway, the SandboxBackend, or agent HTTP; sequencing,
timeouts, retries and compensation live in the workflow, not here.

Retry safety, per activity:
    read_issue        — read plus an idempotent artifact link (COALESCE
                        upsert of the ticket URL); safe.
    fetch_workspace   — read-only; safe.
    provision_sandbox — the backend pre-removes a same-name remnant of a
                        crashed earlier attempt; registration upserts.
    dispatch_agent    — the agent answers 409 to a re-dispatch and the
                        activity falls through to polling; if the agent
                        lost the run (restart), the poll raises and the
                        retry re-dispatches from scratch.
    platform_verify   — read-only against the sandbox; safe.
"""

import asyncio
from typing import Any

from temporalio import activity

from common.telemetry import get_logger
from compiler.infra import DEFAULT_IMAGE
from orchestrator.clients import GatewayClient, dispatch_and_poll
from orchestrator.config import WorkerSettings
from registry import Companies, Runs
from sandbox.contract import SandboxBackend, SandboxPolicy


class ImplementActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        backend: SandboxBackend,
        runs: Runs,
        companies: Companies,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._runs = runs
        # Read-only: the applied blueprint's infra IR is the SandboxPolicy
        # source.
        self._companies = companies
        self._log = get_logger("orchestrator.implement")

    @activity.defn(name="read_issue")
    async def read_issue(self, run_id: str, issue_number: int) -> dict[str, Any]:
        """Issue metadata under the platform identity: title and body seed
        the dispatch text, the URL lands on the run row. Fail-closed on a
        closed issue — implement consumes open work, it does not resurrect
        finished tickets. The body is untrusted input, exactly like a task
        file's contents."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issue = await gateway.read_issue(
                run_id, self._settings.target_repo, issue_number
            )
        if issue.get("state") != "open":
            raise RuntimeError(
                f"issue #{issue_number} is {issue.get('state') or 'unknown'},"
                " not open — nothing to implement"
            )
        await asyncio.to_thread(
            self._runs.record_artifacts, run_id, ticket_urls=[issue["url"]]
        )
        self._log.bind(run_id=run_id).info(
            "issue resolved", extra={"issue_number": issue_number}
        )
        return issue

    def _sandbox_policy(self, run_id: str) -> SandboxPolicy:
        """The compiled SandboxPolicy: the applied blueprint's infra IR plus
        the per-run egress credential and the deploy-time resolution of the
        logical toolchain image. The blueprint's test command rides the pod
        env: the sandbox runs what the company declared, not a default. No applied blueprint means no
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
            env={
                "TEST_CMD": sandbox_ir["test_command"],
                **self._settings.sandbox_proxy_env(run_id),
            },
        )

    @activity.defn(name="fetch_workspace")
    async def fetch_workspace(
        self,
        run_id: str,
        ref: str | None = None,
        overlay_paths: list[str] | None = None,
    ) -> bytes:
        """Archive the target repo at `ref` — the configured base ref by
        default; the review workflow passes the PR head ref instead
        (the review sandbox is a full sandbox from the PR ref). With
        `overlay_paths`, those paths are mirrored from the configured base
        ref over the checkout: the composed tree the contract verify runs —
        head code, the contract tree as it stands on the base ref."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            workspace_tar = await gateway.fetch_workspace(
                run_id,
                self._settings.target_repo,
                ref or self._settings.base_ref,
                self._settings.base_ref if overlay_paths else None,
                overlay_paths,
            )
        self._log.bind(run_id=run_id).info(
            "workspace fetched",
            extra={"bytes": len(workspace_tar), "overlay_paths": overlay_paths},
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

    @activity.defn(name="dispatch_agent")
    async def dispatch_agent(
        self, run_id: str, url: str, procedure: str, task_text: str
    ) -> dict[str, Any]:
        """Ask the agent at `url` — a standing conductor's Service or an
        ephemeral pod — to run one procedure on the text, and poll until it
        reaches a terminal state, heartbeating every poll. The pod's own
        projected ServiceAccount token is the credential: no token rides
        this call or the workflow history. Which procedures reach which
        agent is the workflow's sequencing (`deliver` only after a green
        platform verify) and the role's declared set at the host."""
        status = await dispatch_and_poll(
            url, run_id, procedure, task_text, self._settings.poll_interval_s
        )
        self._log.bind(run_id=run_id).info(
            "agent finished", extra={"agent_status": status, "procedure": procedure}
        )
        return status

    @activity.defn(name="platform_verify")
    async def platform_verify(
        self, run_id: str, paths: list[str] | None = None
    ) -> dict[str, Any]:
        """The gate of record: the platform
        runs the sandbox's test suite under the orchestrator's own
        identity. The implementer's claim is advisory; this result is what
        delivery hangs on. One verification concept, parameterized by
        scope: no `paths` runs the whole suite (implement's verify loop,
        review's hygiene run); with `paths` the run is pinned to those
        test paths — the contract verify, where collecting nothing from a
        named path is a red exit, not a silent pass."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.run_tests(run_id, paths)
        passed = result.get("exit_code") == 0
        output = f"{result.get('stdout', '')}\n{result.get('stderr', '')}".strip()
        self._log.bind(run_id=run_id).info(
            "platform verify", extra={"passed": passed, "paths": paths}
        )
        return {"passed": passed, "output": output[-4000:]}

    def all(self) -> list[Any]:
        return [
            self.read_issue,
            self.fetch_workspace,
            self.provision_sandbox,
            self.dispatch_agent,
            self.platform_verify,
        ]
