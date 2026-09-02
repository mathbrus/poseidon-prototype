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
    read_issue        — read plus an idempotent artifact link (COALESCE
                        upsert of the ticket URL); safe.
    dispatch_agent    — the agent answers 409 to a re-dispatch and the
                        activity falls through to polling; if the agent
                        lost the run (restart), the poll raises and the
                        retry re-dispatches from scratch.
    platform_verify   — read-only against the sandbox; safe.
    teardown          — destroy is idempotent; the participants sweep and
                        the run row finalization are upserts.
    record_attempts_exhausted
                      — an append-only audit write plus one comment; a
                        retry can duplicate both (a second notice on the
                        item, never a lost one).
"""

import asyncio
from typing import TYPE_CHECKING, Any

from temporalio import activity

if TYPE_CHECKING:
    from orchestrator.activities.crew import CrewActivities

from common.audit import args_digest
from common.telemetry import get_logger
from compiler.infra import DEFAULT_IMAGE
from orchestrator.activities.blueprint import workflow_config
from orchestrator.audit import record_event
from orchestrator.clients import GatewayClient, dispatch_and_poll
from orchestrator.config import WorkerSettings
from orchestrator.workflows.base import workflow_id
from registry import (
    ISSUE_TASK_RE,
    REVIEW_TASK_RE,
    AgentInstances,
    AuditStore,
    Companies,
    Runs,
)
from sandbox.contract import SandboxBackend, SandboxHandle, SandboxPolicy

# The process-gate event a run writes when it spent the last attempt the
# blueprint allows on its issue or PR: `gate.heartbeat_attempts` /
# `gate_exhausted`, the shape of the rework bound's `gate_rework_exhausted`
# and the daily cap's `gate_halted`.
ATTEMPTS_GATE = "heartbeat_attempts"


def attempts_exhausted_comment(kind: str, count: int, max_attempts: int) -> str:
    """The notice the platform leaves on the item itself, so the
    abandonment is visible where a human looks — not only in the ledger."""
    item = "pull request" if kind == "review" else "issue"
    return (
        f"Poseidon ran {count} {kind} run(s) on this {item} and none concluded"
        f" — no {'verdict' if kind == 'review' else 'pull request'} was"
        f" reached. That is the blueprint's `heartbeat.max_attempts`"
        f" ({max_attempts}), so the heartbeat will not start another on its"
        f" own. A human can: `poseidon {'review' if kind == 'review' else 'run'}"
        f" <number>`."
    )


class ImplementActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        backend: SandboxBackend,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances,
        audit: AuditStore,
        crew: "CrewActivities | None" = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._runs = runs
        self._audit = audit
        # Read-only: the applied blueprint's infra IR is the SandboxPolicy
        # source, and its workflow config directs the crew-only flow.
        self._companies = companies
        # Read-only: standing_role name -> standing instance (the conductor id the
        # crew request is recorded under).
        self._instances = instances
        # The identity revoke, called directly (same process) from teardown
        # so run-binding termination rides the one compensation anchor that
        # runs on every terminal path.
        self._crew = crew
        self._log = get_logger("orchestrator.activities")

    @activity.defn(name="create_run")
    async def create_run(self, run_id: str, task_text: str) -> None:
        await asyncio.to_thread(
            self._runs.start,
            run_id,
            task_text,
            "implement",
            workflow_id("implement", run_id),
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
            config, conductor = workflow_config(
                self._companies, self._instances, self._settings.company, "implement"
            )
            return {
                **conductor,
                "max_verify_retries": int(config.get("max_verify_retries") or 0),
            }

        return await asyncio.to_thread(read)

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
        identity. The executor's claim is advisory; this result is what
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
        # Handle-independent on purpose: bind precedes provision, so
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
                gone = await self._crew.destroy_ephemeral_pods(run_id)
                if gone:
                    log.info("ephemeral pods destroyed", extra={"pods": gone})
            except Exception as exc:
                log.info("ephemeral pod destroy failed", extra={"error": str(exc)})
                error = error or f"teardown: ephemeral pod destroy failed: {exc}"
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

    @activity.defn(name="record_attempts_exhausted")
    async def record_attempts_exhausted(
        self, run_id: str, workflow_id: str
    ) -> dict[str, Any]:
        """Called after teardown by a run that concluded nothing. If that
        run was the last the blueprint's `heartbeat.max_attempts` allows on
        its issue or PR, say so twice: a `gate.heartbeat_attempts` /
        `gate_exhausted` process-gate event under this run's id, and a
        platform comment on the item, so the world learns the company has
        stopped trying. Nothing to record when the company has no
        heartbeat section (no bound exists), when the run's task is not
        heartbeat-startable work (a task file), when attempts remain, or
        when another run already concluded the item (it is consumed by
        that conclusion, not by exhaustion)."""
        log = self._log.bind(run_id=run_id)

        def read() -> tuple[str, int, int, int] | None:
            run = self._runs.get(run_id)
            if run is None:
                return None
            spec = self._companies.get(self._settings.company) or {}
            max_attempts = (spec.get("heartbeat") or {}).get("max_attempts")
            if max_attempts is None:
                return None
            kind = run.workflow
            pattern, aggregate = {
                "implement": (ISSUE_TASK_RE, self._runs.issue_attempts),
                "review": (REVIEW_TASK_RE, self._runs.review_attempts),
            }.get(kind, (None, None))
            match = pattern.match(run.task) if pattern else None
            if not match:
                return None
            number = int(match.group(1))
            attempts = aggregate(self._settings.company).get(number)
            if attempts is None or attempts.concluded:
                return None
            if attempts.count < int(max_attempts):
                return None
            return kind, number, attempts.count, int(max_attempts)

        found = await asyncio.to_thread(read)
        if found is None:
            return {"exhausted": False}
        kind, number, count, max_attempts = found
        await record_event(
            self._audit,
            run_id=run_id,
            tool=f"gate.{ATTEMPTS_GATE}",
            digest=args_digest(
                {"gate": ATTEMPTS_GATE, "outcome": "exhausted", "attempts": count}
            ),
            decision="deny",
            rule="gate_exhausted",
            source="process-gate",
            company=self._settings.company,
            workflow=workflow_id,
        )
        body = attempts_exhausted_comment(kind, count, max_attempts)
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            if kind == "review":
                await gateway.comment_pr(run_id, self._settings.target_repo, number, body)
            else:
                await gateway.comment_issue(
                    run_id, self._settings.target_repo, number, body
                )
        log.info(
            "attempts exhausted",
            extra={"workflow": kind, "number": number, "attempts": count},
        )
        return {"exhausted": True, "attempts": count}

    def all(self) -> list[Any]:
        return [
            self.create_run,
            self.read_issue,
            self.fetch_workspace,
            self.provision_sandbox,
            self.implement_config,
            self.dispatch_agent,
            self.platform_verify,
            self.teardown,
            self.record_attempts_exhausted,
        ]
