"""The run lifecycle activities every run-shaped workflow shares — scope,
implement and review are born, shaped, gated and torn down the same way.
Each is a thin client over the registry, the sandbox backend or the
gateway; sequencing, timeouts, retries and compensation live in the
workflow, not here.

Retry safety, per activity:
    create_run          — upserts the run row; safe.
    check_budget        — read-only; safe. The activity-boundary headroom
                          check: enforcement stays local at the LLM
                          gateway, but a run that would be born exhausted
                          fails here, before any dispatch.
    workflow_config     — read-only (the applied blueprint and the
                          converged conductor instance); safe.
    record_gate_outcome — appends an audit event; a retried append
                          duplicates a row, never loses one (the audit
                          store is append-only by design).
    teardown            — destroy is idempotent; the participants sweep and
                          the run row finalization are upserts.
    record_attempts_exhausted
                        — an append-only audit write plus one comment; a
                          retry can duplicate both (a second notice on the
                          item, never a lost one).
    read_documents      — read-only (the company's documents at base_ref
                          under the platform identity); safe.
"""

import asyncio
from dataclasses import asdict
from typing import TYPE_CHECKING, Any

from temporalio import activity

if TYPE_CHECKING:
    from orchestrator.activities.crew import CrewActivities

from common.audit import args_digest, gate_event
from common.tasks import ISSUE_TASK_RE, REVIEW_TASK_RE
from common.telemetry import get_logger
from common.workflows import ATTEMPTS_GATE, workflow_id
from orchestrator.activities.blueprint import company_repo, workflow_config
from orchestrator.activities.company import applied_company
from orchestrator.activities.documents import (
    company_documents,
    present_texts,
    read_documents,
)
from orchestrator.audit import record_event
from orchestrator.clients import GatewayClient
from orchestrator.config import WorkerSettings
from orchestrator.workflows.base import PROFILES
from registry import (
    AgentInstances,
    AuditStore,
    BudgetLedger,
    Companies,
    Runs,
)
from sandbox.contract import SandboxBackend, SandboxHandle


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


class RunActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        backend: SandboxBackend,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances,
        ledger: BudgetLedger,
        audit: AuditStore,
        crew: "CrewActivities | None" = None,
    ) -> None:
        self._settings = settings
        self._backend = backend
        self._runs = runs
        # Read-only: the applied blueprint shapes every run, and the
        # converged standing instances resolve its conductor.
        self._companies = companies
        self._instances = instances
        self._ledger = ledger
        self._audit = audit
        # The identity revoke, called directly (same process) from teardown
        # so run-binding termination rides the one compensation anchor that
        # runs on every terminal path.
        self._crew = crew
        self._log = get_logger("orchestrator.run")

    async def _repo(self) -> dict[str, str]:
        """The company's repository, from its applied blueprint."""
        return await asyncio.to_thread(company_repo, self._companies, self._company())

    def _company(self) -> str:
        return applied_company(self._companies)

    @activity.defn(name="create_run")
    async def create_run(self, run_id: str, workflow: str, task: str) -> None:
        """The run's row: which workflow owns it and the task it works
        (`common.tasks`: the item's key, or scope's fixed label)."""
        await asyncio.to_thread(
            self._runs.start,
            run_id,
            task,
            workflow,
            workflow_id(workflow, run_id),
            self._company(),
        )
        self._log.bind(run_id=run_id).info("run created", extra={"workflow": workflow})

    @activity.defn(name="check_budget")
    async def check_budget(self, run_id: str) -> dict[str, Any]:
        """Budget reservation at the run's birth: verify the run has
        headroom under the applied blueprint's per-run cap before anything
        is dispatched. Enforcement stays local at the LLM gateway —
        this is the activity-boundary observation of the same ledger."""

        def read() -> tuple[float, float]:
            spec = self._companies.get(self._company()) or {}
            cap = float(((spec.get("budgets") or {}).get("per_run_usd")) or 0)
            return cap, self._ledger.spent_for_run(run_id)

        cap, spent = await asyncio.to_thread(read)
        if cap > 0 and spent >= cap:
            raise RuntimeError(
                f"budget_exhausted: run {run_id} has no headroom"
                f" ({spent:.6f} of {cap:.6f} USD spent)"
            )
        return {"cap_usd": cap, "spent_usd": spent}

    @activity.defn(name="workflow_config")
    async def workflow_config(
        self, workflow: str, crewless: bool = False
    ) -> dict[str, Any]:
        """The named workflow's shape, read from the applied blueprint and
        the converged conductor instance — gates, crew, bounds and the
        conductor all come from config, never from code — plus the
        deployment's timeout profile (`timeouts`). Fail-closed: no
        applied blueprint, no node for a crewed workflow (`crewless` is
        the workflow's own word that it has none), or no converged
        conductor each refuse the run before anything is provisioned."""
        config = await asyncio.to_thread(
            workflow_config,
            self._companies,
            self._instances,
            self._company(),
            workflow,
            crewless,
        )
        return {**config, "timeouts": asdict(PROFILES[self._settings.timeout_profile])}

    @activity.defn(name="dispatch_documents")
    async def dispatch_documents(self, run_id: str) -> dict[str, str]:
        """The company's documents as a dispatch carries them: name to
        text for the ones present on `base_ref`, read under the run's own
        id — never the sandbox's copy, which the run could have edited."""
        paths = await asyncio.to_thread(
            company_documents, self._companies, self._company()
        )
        repo = await self._repo()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            documents = await read_documents(gateway, run_id, repo, paths)
        return present_texts(documents)

    @activity.defn(name="record_gate_outcome")
    async def record_gate_outcome(
        self, run_id: str, gate: str, kind: str, workflow_id: str
    ) -> None:
        """One `source=process-gate` audit event: parked gates,
        escalations and verdicts become first-class rows in the run's
        story, carrying the workflow id that maps back to run_id. The
        policy kinds keep human and policy resolutions distinguishable
        forever: `policy_resolved` released a merge, `policy_declined`
        fell back to the human signal."""
        await record_event(
            self._audit,
            run_id=run_id,
            tool=gate_event(gate),
            digest=args_digest({"gate": gate, "outcome": kind}),
            decision="allow" if kind in ("approved", "policy_resolved") else "deny",
            rule=f"gate_{kind}",
            source="process-gate",
            company=self._company(),
            workflow=workflow_id,
        )
        self._log.bind(run_id=run_id).info(
            "gate outcome recorded", extra={"gate": gate, "outcome": kind}
        )

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
        was provisioned), retire its registration, link the artifacts the
        run produced, and write the honest terminal run record, which is
        also the workflow's result."""
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
        ticket_urls = list(result.get("ticket_urls") or [])
        if ticket_urls:
            await asyncio.to_thread(
                self._runs.record_artifacts, run_id, None, ticket_urls
            )
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
        stopped trying. Nothing to record when the run worked no item (a
        scope run), when attempts remain, or when another run already
        concluded the item (it is consumed by that conclusion, not by
        exhaustion)."""
        log = self._log.bind(run_id=run_id)

        def read() -> tuple[str, int, int, int] | None:
            run = self._runs.get(run_id)
            if run is None:
                return None
            spec = self._companies.get(self._company())
            if not spec:
                return None
            max_attempts = spec["heartbeat"]["max_attempts"]
            kind = run.workflow
            pattern, aggregate = {
                "implement": (ISSUE_TASK_RE, self._runs.issue_attempts),
                "review": (REVIEW_TASK_RE, self._runs.review_attempts),
            }.get(kind, (None, None))
            match = pattern.match(run.task) if pattern else None
            if not match:
                return None
            number = int(match.group(1))
            attempts = aggregate(self._company()).get(number)
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
            tool=gate_event(ATTEMPTS_GATE),
            digest=args_digest(
                {"gate": ATTEMPTS_GATE, "outcome": "exhausted", "attempts": count}
            ),
            decision="deny",
            rule="gate_exhausted",
            source="process-gate",
            company=self._company(),
            workflow=workflow_id,
        )
        body = attempts_exhausted_comment(kind, count, max_attempts)
        repo = (await self._repo())["url"]
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            if kind == "review":
                await gateway.comment_pr(run_id, repo, number, body)
            else:
                await gateway.comment_issue(run_id, repo, number, body)
        log.info(
            "attempts exhausted",
            extra={"workflow": kind, "number": number, "attempts": count},
        )
        return {"exhausted": True, "attempts": count}

    def all(self) -> list[Any]:
        return [
            self.create_run,
            self.check_budget,
            self.workflow_config,
            self.dispatch_documents,
            self.record_gate_outcome,
            self.teardown,
            self.record_attempts_exhausted,
        ]
