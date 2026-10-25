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
from temporalio.exceptions import ApplicationError

from common.telemetry import get_logger
from common.topology import SANDBOX_TOOLCHAIN
from common.verify import suite_green
from common.workflows import MERGE_CONFLICT
from orchestrator.activities.blueprint import company_repo
from orchestrator.activities.company import applied_company
from orchestrator.clients import GatewayClient, GatewayError, dispatch_and_poll
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

    async def _repo(self) -> dict[str, str]:
        """The company's repository, from its applied blueprint."""
        return await asyncio.to_thread(company_repo, self._companies, self._company())

    def _company(self) -> str:
        return applied_company(self._companies)

    @activity.defn(name="read_issue")
    async def read_issue(self, run_id: str, issue_number: int) -> dict[str, Any]:
        """Issue metadata under the platform identity: title and body seed
        the dispatch text, the URL lands on the run row, and the ticket's
        refused deliveries (`refusals`, oldest first) tell this attempt what
        went wrong before. Fail-closed on a closed issue — implement
        consumes open work, it does not resurrect finished tickets. The
        body and the refusals' reasons are untrusted input, like every
        text an agent reads."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issue = await gateway.read_issue(
                run_id, (await self._repo())["url"], issue_number
            )
        if issue.get("state") != "open":
            raise RuntimeError(
                f"issue #{issue_number} is {issue.get('state') or 'unknown'},"
                " not open — nothing to implement"
            )
        await asyncio.to_thread(
            self._runs.record_artifacts, run_id, ticket_urls=[issue["url"]]
        )
        refusals = await asyncio.to_thread(
            self._runs.refusals, self._company(), issue_number
        )
        issue["refusals"] = [{"pr_url": url, "reason": why} for url, why in refusals]
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
        spec = self._companies.get(self._company())
        sandbox_ir = ((spec or {}).get("infra") or {}).get("sandbox")
        if not sandbox_ir:
            raise RuntimeError(
                f"no applied blueprint for company {self._company()!r}"
                " — provision_sandbox has no compiled SandboxPolicy source"
            )
        # The platform's own toolchain is named logically in the blueprint;
        # only the deployment knows the content-addressed image the cluster
        # carries. Any other image is used as the blueprint wrote it.
        image = sandbox_ir["image"]
        if image == SANDBOX_TOOLCHAIN:
            image = self._settings.sandbox_image
        return SandboxPolicy(
            image=image,
            workspace_dir=sandbox_ir["workspace_dir"],
            cpu=sandbox_ir["cpu"],
            memory=sandbox_ir["memory"],
            disk=sandbox_ir["disk"],
            deadline_s=int(sandbox_ir["deadline_s"]),
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
        merge_onto: str | None = None,
    ) -> bytes:
        """Archive the target repo at `ref` — the configured base ref by
        default; the review workflow passes the PR head ref instead
        (the review sandbox is a full sandbox from the PR ref). With
        `overlay_paths`, those paths are mirrored from the configured base
        ref over the checkout: the composed tree the contract verify runs —
        head code, the contract tree as it stands on the base ref. With
        `merge_onto`, `ref` merged onto that commit: the tree a merge onto
        a moved base would land. A head that does not merge cleanly is a
        non-retryable `MERGE_CONFLICT` that names the conflict."""
        repo = await self._repo()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            try:
                workspace_tar = await gateway.fetch_workspace(
                    run_id,
                    repo["url"],
                    ref or repo["base_ref"],
                    repo["base_ref"] if overlay_paths else None,
                    overlay_paths,
                    merge_onto,
                )
            except GatewayError as exc:
                said = str(exc)
                if merge_onto and MERGE_CONFLICT in said:
                    # The conflict as the gateway named it, without the tool
                    # call's error prefix: the refusal reason a person reads.
                    raise ApplicationError(
                        said[said.index(MERGE_CONFLICT) :],
                        type=MERGE_CONFLICT,
                        non_retryable=True,
                    ) from None
                raise
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
        self,
        run_id: str,
        url: str,
        procedure: str,
        task_text: str,
        bounds: str,
        documents: dict[str, str],
    ) -> dict[str, Any]:
        """Ask the agent at `url` — a standing conductor's Service or an
        ephemeral pod — to run one procedure on the text, with the bounds
        the run's shape rendered and the company's documents, and poll
        until it reaches a terminal state, heartbeating every poll. The
        pod's own projected ServiceAccount token is the credential: no
        token rides this call or the workflow history. Which procedures
        reach which agent is the workflow's sequencing (`deliver` only
        after a green platform verify) and the role's declared set at the
        host."""
        status = await dispatch_and_poll(
            url, run_id, procedure, task_text, bounds, documents
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
        test paths — the contract verify. What the exit means by scope is
        `common.verify`'s rule."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.run_tests(run_id, paths)
        passed = suite_green(result.get("exit_code"), scoped=bool(paths))
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
