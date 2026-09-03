"""Crew fulfillment activities: identity binding and pod provisioning.

Bind writes the `run_participants` binding row (the fulfill half of the
ephemeral-identity contract; the gateways own the verify half): the row
maps the agent's per-run ServiceAccount to its run, crew, company and
catalog role. Revoke terminates rows — the write that
kills the identity on its next gateway call, however long its projected
tokens live.

Provision creates each ephemeral agent's pod (and its
ServiceAccount) through the bounded `EphemeralPodProvisioner`; the pod's only
credential is the projected token the kubelet mounts, so no credential
ever rides workflow history or a pod env. Destroy deletes by the
run+kind label selector.

Everything emits `process-lifecycle` audit events onto the shared audit
ingestion contract, so a run's story shows who was bound, provisioned,
revoked and destroyed, when, under whose authority. `fulfill_crew` composes
these into the fulfill orchestration; nothing else here dispatches work.
"""

import asyncio
import time
from typing import Any

from temporalio import activity
from temporalio.exceptions import ApplicationError

from common.audit import args_digest
from common.telemetry import get_logger
from common.topology import TOKEN_FILE, llm_gateway_url, mcp_gateway_url
from orchestrator.audit import record_event
from orchestrator.config import WorkerSettings
from orchestrator.ephemeral_pods import (
    EphemeralPodProvisioner,
    service_account_name,
)
from orchestrator.identity import ephemeral_agent_id
from orchestrator.opa import evaluate_decision
from registry import (
    AuditStore,
    BudgetLedger,
    Companies,
    CrewRequests,
    RunParticipants,
    Runs,
)
from registry.crews import request_id


class CrewActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        participants: RunParticipants,
        audit: AuditStore,
        crew_requests: CrewRequests,
        companies: Companies,
        ledger: BudgetLedger,
        runs: Runs,
        provisioner: EphemeralPodProvisioner | None = None,
    ) -> None:
        self._settings = settings
        self._participants = participants
        self._audit = audit
        self._provisioner = provisioner
        # Fulfillment: the recorded intent it consumes, and the
        # applied blueprint the crew's agent specs come from.
        self._crew_requests = crew_requests
        self._companies = companies
        # Read-only, for the workflow-recorded crew request: the
        # budget-headroom half of the decision-input enrichment.
        self._ledger = ledger
        # Read-only, for the terminal-run guard: a run that has
        # already finalized must never acquire new pods or identities.
        self._runs = runs
        self._log = get_logger("orchestrator.crew")

    @activity.defn(name="bind_participant")
    async def bind_participant(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Bind one ephemeral agent to one run: the participants row *is* the
        identity — it maps the agent's per-run ServiceAccount to its run,
        crew, company and catalog role, and the gateways resolve the SA
        against it on every call. Nothing is signed here.

        spec: {run_id, company, crew_template, role, parent_agent_id}.
        Retry-safe: the agent_id derives from {crew, role, run}, so a
        re-bind lands on the same row — one identity, never two. A re-bind
        after the run's teardown swept the row raises `ParticipantRevoked`:
        revocation is final.
        """
        started = time.monotonic()
        agent_id = ephemeral_agent_id(spec["crew_template"], spec["role"], spec["run_id"])
        await asyncio.to_thread(self._bind_row, agent_id, spec)
        await self._emit(
            run_id=spec["run_id"],
            agent_id=agent_id,
            role=spec["role"],
            company=spec["company"],
            parent_agent_id=spec["parent_agent_id"],
            tool="lifecycle.bind",
            rule="participant_bound",
            digest=args_digest(spec),
            started=started,
        )
        self._log.bind(run_id=spec["run_id"]).info(
            "participant bound", extra={"agent_id": agent_id}
        )
        return {
            "agent_id": agent_id,
            "service_account": service_account_name(agent_id),
        }

    def _bind_row(self, agent_id: str, spec: dict[str, Any]) -> None:
        """The single place the SA-name <-> agent_id mapping is written.
        The namespace recorded is where the provisioner puts ephemeral pods and
        their ServiceAccounts — the gateway refuses the same SA name
        anywhere else."""
        self._participants.add(
            agent_id,
            spec["run_id"],
            spec["parent_agent_id"],
            spec["crew_template"],
            spec["role"],
            company=spec["company"],
            namespace=self._settings.company,
            service_account=service_account_name(agent_id),
        )

    @activity.defn(name="revoke_participants")
    async def revoke_participants(self, run_id: str) -> list[str]:
        """Terminate every live participant of the run — the terminal-path
        sweep. From these writes on, the gateways find no live rows and
        every crew identity of the run is dead, whatever its projected
        tokens' remaining validity."""
        started = time.monotonic()
        swept = await asyncio.to_thread(self._participants.terminate_for_run, run_id)
        for agent_id in swept:
            await self._emit(
                run_id=run_id,
                agent_id=agent_id,
                role="",
                company="",
                parent_agent_id="",
                tool="lifecycle.revoke",
                rule="participant_revoked",
                digest=args_digest({"run_id": run_id}),
                started=started,
            )
        self._log.bind(run_id=run_id).info(
            "participants revoked", extra={"agent_ids": swept}
        )
        return swept

    @activity.defn(name="provision_ephemeral_pod")
    async def provision_ephemeral_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the ephemeral agent's pod and its ServiceAccount.
        The pod's only credential is the projected token the kubelet
        mounts — nothing token-shaped enters the env or workflow history.

        spec: {run_id, company, crew_template, role, parent_agent_id}.
        Re-asserting the binding row lets a retry after a crash converge on
        the same identity; a retry that lost the race with its run's
        teardown raises `ParticipantRevoked` instead.
        """
        started = time.monotonic()
        provisioner = self._require_provisioner()
        agent_id = ephemeral_agent_id(spec["crew_template"], spec["role"], spec["run_id"])
        await asyncio.to_thread(self._bind_row, agent_id, spec)
        platform = self._settings.platform_namespace
        env = {
            "MCP_GATEWAY_URL": mcp_gateway_url(platform),
            "LLM_PROXY_URL": llm_gateway_url(platform),
            "MODEL": self._settings.model,
            "ROLE": spec["role"],
            "TARGET_REPO": self._settings.target_repo,
            "VERIFY_CYCLES": str(self._settings.verify_cycles),
            "GATEWAY_TOKEN_FILE": TOKEN_FILE,
        }
        # The teardown race guard: a retry still in flight while the run
        # ends must not leak a pod. `still_wanted` checks two signals —
        # the live participant row and the run's non-terminal state; see
        # `_collectable_now` for why both are needed. The provisioner
        # evaluates it immediately before pod creation (not here, where
        # the verdict would go stale during its remnant-deletion wait, and
        # not after, where the workflow's completion would cancel this
        # activity first).
        handle = await self._heartbeating(
            lambda: provisioner.provision(
                agent_id=agent_id,
                run_id=spec["run_id"],
                company=spec["company"],
                agent_image=self._settings.agent_image,
                env=env,
                still_wanted=lambda: self._collectable_now(spec["run_id"], agent_id),
            )
        )
        await self._emit(
            run_id=spec["run_id"],
            agent_id=agent_id,
            role=spec["role"],
            company=spec["company"],
            parent_agent_id=spec["parent_agent_id"],
            tool="lifecycle.provision_pod",
            rule="ephemeral_pod_provisioned",
            digest=args_digest(spec),
            started=started,
        )
        self._log.bind(run_id=spec["run_id"]).info(
            "ephemeral pod provisioned", extra={"agent_id": agent_id, "url": handle.url}
        )
        return {"agent_id": agent_id, "pod_name": handle.pod_name, "url": handle.url}

    async def _heartbeating(self, fn: Any) -> Any:
        """Run a blocking call on a worker thread, heartbeating while it
        runs. Pod provisioning (remnant wait + image start + readiness)
        legitimately outlives the activity heartbeat window — and
        `fulfill_crew` provisions one pod per agent back to back — so
        liveness must be signalled *during* the call, not only between
        calls."""
        task = asyncio.ensure_future(asyncio.to_thread(fn))
        while True:
            done, _ = await asyncio.wait({task}, timeout=5.0)
            if activity.in_activity():
                activity.heartbeat()
            if done:
                return task.result()

    def _collectable_now(self, run_id: str, agent_id: str) -> bool:
        """Whether anything provisioned for this run would still be torn
        down by someone. False once the run's teardown has swept past.

        Two signals, because either alone has a hole: a revoked binding
        row means the sweep already passed (teardown revokes before it
        destroys), but the row can be re-created by a provision retry —
        the run's own terminal state cannot. Synchronous: the provisioner
        calls it from the worker thread, immediately before creating the
        pod."""
        if self._participants.active(agent_id) is None:
            return False
        record = self._runs.get(run_id)
        # A missing run row is not evidence of finalization (the e2e
        # fixtures delete rows); only an explicitly terminal one is.
        return record is None or record.status not in ("done", "failed")

    @activity.defn(name="destroy_ephemeral_pods")
    async def destroy_ephemeral_pods(self, run_id: str) -> list[str]:
        """Delete the run's ephemeral pods — by the {run_id, kind} label
        selector only — the provisioner's bound. Idempotent; audited per pod. With no
        provisioner configured nothing could ever have been provisioned, so
        compensation is honestly a no-op."""
        if self._provisioner is None:
            return []
        started = time.monotonic()
        deleted = await asyncio.to_thread(self._provisioner.destroy, run_id)
        for name in deleted:
            await self._emit(
                run_id=run_id,
                agent_id=name,
                role="",
                company="",
                parent_agent_id="",
                tool="lifecycle.destroy_pod",
                rule="ephemeral_pod_destroyed",
                digest=args_digest({"run_id": run_id}),
                started=started,
            )
        return deleted

    @activity.defn(name="fulfill_crew")
    async def fulfill_crew(self, spec: dict[str, Any]) -> dict[str, Any]:
        """The fulfillment composition: consume the recorded request, then
        bind every agent of the crew and give each its own pod (one
        principal, one pod, one ServiceAccount). Returns the agents by
        role, each with its identity and dispatch address; the workflow
        dispatches, this activity never does.

        spec: {run_id, company, crew_template, conductor: {agent_id,
        role, url}, workflow, record_under_authority?, reason?}.
        `workflow` names the calling workflow in the recorded decision
        input. A workflow whose blueprint binds no crew is refused here,
        before anything is provisioned — execution is ephemeral crews, and
        the compiler should already have refused such a blueprint.
        Retry-safe end to end: consume is idempotent for this run, binding
        revives deterministic identities, provisioning replaces remnants.
        """
        run_id = spec["run_id"]
        crew_template = spec["crew_template"]
        if not crew_template:
            raise ApplicationError(
                f"{spec['workflow']} workflow binds no crew — execution is"
                " ephemeral crews; the compiler should have refused this blueprint",
                non_retryable=True,
            )
        conductor = spec["conductor"]["agent_id"]
        conductor_role = spec["conductor"]["role"]
        started = time.monotonic()

        rid = request_id(conductor, crew_template, run_id)
        consumed = await asyncio.to_thread(self._crew_requests.consume, rid, run_id)
        if consumed is None and spec.get("record_under_authority"):
            # The workflow path: record the request under the
            # conductor's authority — same row, same policy. "Same policy"
            # is literal: the recording is allowed only if the compiled
            # decision the crew.request *tool* faces allows it, evaluated
            # against the same bundle with the same enriched input.
            await self._authorize_recording(
                run_id,
                spec["company"],
                conductor,
                conductor_role,
                crew_template,
                spec["workflow"],
            )
            await asyncio.to_thread(
                self._crew_requests.record,
                conductor,
                crew_template,
                run_id,
                spec.get("reason", "workflow-recorded under conductor authority"),
            )
            consumed = await asyncio.to_thread(self._crew_requests.consume, rid, run_id)
        if consumed is None:
            # No recorded, unexpired intent for this run: fulfillment does
            # not improvise authority (expired requests orphan nothing).
            raise RuntimeError(
                f"no recorded crew request for {conductor}/{crew_template}"
                f" on run {run_id} — record intent before fulfilling"
            )

        blueprint = await asyncio.to_thread(self._companies.get, spec["company"]) or {}
        agents = [
            m
            for m in blueprint.get("ephemeralAgents") or []
            if m.get("crew") == crew_template
        ]
        if len(agents) != 1:
            # Provisioning handles any number of agents, but every workflow
            # dispatches exactly one; a crew declaring more has no defined
            # dispatch, so it is refused before any pod exists.
            raise RuntimeError(
                f"crew {crew_template!r} declares {len(agents)} agents; the"
                " applied blueprint must give a crew exactly one"
            )

        # Every agent gets its own pod: identity is per-pod (one
        # ServiceAccount each), so there is no pod to borrow and no token
        # to hand around.
        provisioned: dict[str, dict[str, Any]] = {}
        for agent in agents:
            agent_spec = {
                "run_id": run_id,
                "company": spec["company"],
                "crew_template": crew_template,
                "role": agent["role"],
                "parent_agent_id": conductor,
            }
            await self.bind_participant(agent_spec)
            pod = await self.provision_ephemeral_pod(agent_spec)
            provisioned[agent["role"]] = {"agent_id": pod["agent_id"], "url": pod["url"]}
            await self._emit(
                run_id=run_id,
                agent_id=pod["agent_id"],
                role=agent["role"],
                company=spec["company"],
                parent_agent_id=conductor,
                tool="lifecycle.fulfill",
                rule="crew_fulfilled",
                digest=args_digest({"crew_template": crew_template, "request_id": rid}),
                started=started,
            )
        return {"request_id": rid, "agents": provisioned}

    async def _authorize_recording(
        self,
        run_id: str,
        company: str,
        conductor: str,
        conductor_role: str,
        crew_template: str,
        workflow: str,
    ) -> None:
        """Evaluate the conductor-shaped crew.request decision against the
        live compiled bundle — the "same policy" clause, literally:
        may_spawn narrowing, the concurrency cap and budget headroom all
        bind exactly as at the gateway's crew.request tool. The decision is
        audited either way; a deny refuses the recording."""

        def enrich() -> tuple[int, bool]:
            spawns = self._participants.active_spawn_count(conductor)
            spec = self._companies.get(company) or {}
            cap = float(((spec.get("budgets") or {}).get("per_run_usd")) or 0)
            headroom = True
            if cap > 0:
                headroom = self._ledger.spent_for_run(run_id) < cap
            return spawns, headroom

        spawns, headroom = await asyncio.to_thread(enrich)
        decision_input = {
            "company": company,
            "agent_id": conductor,
            "lifecycle": "standing",
            "parent_agent_id": "",
            "role": conductor_role,
            "run_id": run_id,
            "workflow": workflow,
            "tool": "crew.request",
            "args": {"crew_template": crew_template},
            "run_binding_verified": False,
            "active_spawns": spawns,
            "budget_headroom": headroom,
        }
        result = await evaluate_decision(self._settings.opa_url, company, decision_input)
        started = time.monotonic()
        allowed = bool(result.get("allow"))
        await self._emit(
            run_id=run_id,
            agent_id=conductor,
            role=conductor_role,
            company=company,
            parent_agent_id="",
            tool="lifecycle.record_request",
            rule=str(result.get("rule") or ""),
            digest=args_digest({"crew_template": crew_template}),
            started=started,
            decision="allow" if allowed else "deny",
            lifecycle="standing",
        )
        if not allowed:
            raise RuntimeError(
                f"crew request refused by policy rule"
                f" {result.get('rule')!r} for {conductor}/{crew_template}"
            )

    def _require_provisioner(self) -> EphemeralPodProvisioner:
        if self._provisioner is None:
            raise RuntimeError(
                "no ephemeral-pod provisioner configured — the worker constructs"
                " one for the company namespace"
            )
        return self._provisioner

    async def _emit(
        self,
        *,
        run_id: str,
        agent_id: str,
        role: str,
        company: str,
        parent_agent_id: str,
        tool: str,
        rule: str,
        digest: str,
        started: float,
        decision: str = "allow",
        lifecycle: str = "ephemeral",
    ) -> None:
        event = await record_event(
            self._audit,
            run_id=run_id,
            agent_id=agent_id,
            role=role,
            tool=tool,
            digest=digest,
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status="ok",
            company=company,
            lifecycle=lifecycle,
            parent_agent_id=parent_agent_id,
        )
        self._log.info("audit", extra=event.model_dump())

    def all(self) -> list[Any]:
        return [
            self.bind_participant,
            self.revoke_participants,
            self.provision_ephemeral_pod,
            self.destroy_ephemeral_pods,
            self.fulfill_crew,
        ]
