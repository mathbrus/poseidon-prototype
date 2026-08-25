"""Crew fulfillment activities: identity binding and pod provisioning.

Bind writes the `run_participants` binding row (the fulfill half of the
ephemeral-identity contract; the gateways own the verify half): the row
maps the member's per-run ServiceAccount to its run, crew coordinates,
company and catalog template. Revoke terminates rows — the write that
kills the identity on its next gateway call, however long its projected
tokens live.

Provision creates each crew member's brain-host pod (and its
ServiceAccount) through the bounded `CrewPodProvisioner`; the pod's only
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

from common.audit import args_digest
from common.telemetry import get_logger
from orchestrator.audit import record_event
from orchestrator.clients import dispatch_and_poll
from orchestrator.config import WorkerSettings
from orchestrator.crewpods import (
    TOKEN_MOUNT_DIR,
    CrewPodProvisioner,
    service_account_name,
)
from orchestrator.identity import ephemeral_agent_id
from orchestrator.opa import evaluate_decision
from registry import AuditStore, BudgetLedger, CrewRequests, RunParticipants, Runs
from registry.bundles import Companies
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
        provisioner: CrewPodProvisioner | None = None,
    ) -> None:
        self._settings = settings
        self._participants = participants
        self._audit = audit
        self._provisioner = provisioner
        # Fulfillment: the recorded intent it consumes, and the
        # applied blueprint the crew's member specs come from.
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
        """Bind one crew member to one run: the participants row *is* the
        identity — it maps the member's per-run ServiceAccount to its run,
        crew coordinates, company and catalog template, and the gateways
        resolve the SA against it on every call. Nothing is signed here.

        spec: {run_id, company, crew_template, member, role_template,
        parent_agent_id}. Retry-safe: the agent_id derives from
        {crew, member, run}, so a re-bind lands on the same row — one
        identity, never two. A re-bind after the run's teardown swept the
        row raises `ParticipantRevoked`: revocation is final.
        """
        started = time.monotonic()
        agent_id = ephemeral_agent_id(
            spec["crew_template"], spec["member"], spec["run_id"]
        )
        await asyncio.to_thread(self._bind_row, agent_id, spec)
        await self._emit(
            run_id=spec["run_id"],
            agent_id=agent_id,
            role=spec["role_template"],
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
        The namespace recorded is where the provisioner puts crew pods and
        their ServiceAccounts — the gateway refuses the same SA name
        anywhere else."""
        self._participants.add(
            agent_id,
            spec["run_id"],
            spec["parent_agent_id"],
            spec["crew_template"],
            spec["member"],
            company=spec["company"],
            role_template=spec["role_template"],
            namespace=self._settings.sandbox_namespace,
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

    @activity.defn(name="provision_crew_pod")
    async def provision_crew_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the crew member's brain-host pod and its ServiceAccount.
        The pod's only credential is the projected token the kubelet
        mounts — nothing token-shaped enters the env or workflow history.

        spec: {run_id, company, crew_template, member, role_template,
        parent_agent_id}. Re-asserting the binding row lets a retry after
        a crash converge on the same identity; a retry that lost the race
        with its run's teardown raises `ParticipantRevoked` instead.
        """
        started = time.monotonic()
        provisioner = self._require_provisioner()
        agent_id = ephemeral_agent_id(
            spec["crew_template"], spec["member"], spec["run_id"]
        )
        await asyncio.to_thread(self._bind_row, agent_id, spec)
        platform = self._settings.platform_namespace
        env = {
            "MCP_GATEWAY_URL": f"http://mcp-gateway.{platform}.svc.cluster.local:8080",
            "LLM_PROXY_URL": f"http://llm-gateway.{platform}.svc.cluster.local:4000",
            "MODEL": self._settings.model,
            "TARGET_REPO": self._settings.target_repo,
            "MAX_ITERATIONS": str(self._settings.max_iterations),
            "GATEWAY_TOKEN_FILE": f"{TOKEN_MOUNT_DIR}/token",
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
            role=spec["role_template"],
            company=spec["company"],
            parent_agent_id=spec["parent_agent_id"],
            tool="lifecycle.provision_pod",
            rule="crew_pod_provisioned",
            digest=args_digest(spec),
            started=started,
        )
        self._log.bind(run_id=spec["run_id"]).info(
            "crew pod provisioned", extra={"agent_id": agent_id, "url": handle.url}
        )
        return {"agent_id": agent_id, "pod_name": handle.pod_name, "url": handle.url}

    async def _heartbeating(self, fn: Any) -> Any:
        """Run a blocking call on a worker thread, heartbeating while it
        runs. Pod provisioning (remnant wait + image start + readiness)
        legitimately outlives the activity heartbeat window — and
        `fulfill_crew` provisions one pod per member back to back — so
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

    @activity.defn(name="destroy_crew_pods")
    async def destroy_crew_pods(self, run_id: str) -> list[str]:
        """Delete the run's crew pods — by the {run_id, kind} label
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
                rule="crew_pod_destroyed",
                digest=args_digest({"run_id": run_id}),
                started=started,
            )
        return deleted

    @activity.defn(name="fulfill_crew")
    async def fulfill_crew(self, spec: dict[str, Any]) -> dict[str, Any]:
        """The fulfillment composition: consume the recorded request ->
        bind every member and give each its own pod (one principal, one
        pod, one ServiceAccount) -> dispatch the planner on its own brain
        host -> hand back the brief for the executor dispatch. The
        orchestrator-fixed graph IS the planner->executor handoff; there is
        no free-form crew chat.

        spec: {run_id, company, crew_template, conductor, task_text,
        workflow?}. `workflow` names the calling workflow in the recorded
        decision input (implement by default, or review).
        Retry-safe end to end: consume is idempotent for this run, binding
        revives deterministic identities, provisioning replaces remnants.
        """
        run_id = spec["run_id"]
        crew_template = spec["crew_template"]
        conductor = spec["conductor"]
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
                spec.get("conductor_role", "pm"),
                crew_template,
                spec.get("workflow", "implement"),
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
        members = [
            m
            for m in blueprint.get("crewMembers") or []
            if m.get("crew") == crew_template
        ]
        if not members:
            raise RuntimeError(
                f"applied blueprint defines no members for crew {crew_template!r}"
            )

        # Every member gets its own pod: identity is per-pod (one
        # ServiceAccount each), so there is no pod to borrow and no token
        # to hand around. `needs_sandbox` keeps meaning what it says — the
        # member works the run's sandbox through the gateway — and the
        # first such member is the acting executor the workflow dispatches.
        bound: dict[str, dict[str, Any]] = {}
        pods: dict[str, dict[str, Any]] = {}
        executor: dict[str, Any] | None = None
        for member in members:
            member_spec = {
                "run_id": run_id,
                "company": spec["company"],
                "crew_template": crew_template,
                "member": member["member"],
                "role_template": member["role_template"],
                "parent_agent_id": conductor,
            }
            bound[member["member"]] = await self.bind_participant(member_spec)
            pods[member["member"]] = await self.provision_crew_pod(member_spec)
            if member.get("needs_sandbox") and executor is None:
                executor = member
        if executor is None:
            raise RuntimeError(f"crew {crew_template!r} has no sandbox-facing member")
        pod = pods[executor["member"]]

        # The planner runs on its own brain host under its own identity:
        # its gateway calls carry its own projected token, so its compiled
        # read-only scope binds.
        brief = spec["task_text"]
        planner = next((m for m in members if not m.get("needs_sandbox")), None)
        if planner is not None:
            plan_run = await dispatch_and_poll(
                pods[planner["member"]]["url"],
                run_id,
                spec["task_text"],
                "plan",
                self._settings.poll_interval_s,
            )
            if plan_run.get("error"):
                raise RuntimeError(f"planner failed: {plan_run['error']}")
            brief = plan_run.get("brief") or spec["task_text"]

        await self._emit(
            run_id=run_id,
            agent_id=pod["agent_id"],
            role=executor["role_template"],
            company=spec["company"],
            parent_agent_id=conductor,
            tool="lifecycle.fulfill",
            rule="crew_fulfilled",
            digest=args_digest({"crew_template": crew_template, "request_id": rid}),
            started=started,
        )
        return {
            "request_id": rid,
            "brief": brief,
            "executor_agent_id": pod["agent_id"],
            "executor_url": pod["url"],
            "members": sorted(bound),
        }

    async def _authorize_recording(
        self,
        run_id: str,
        company: str,
        conductor: str,
        conductor_role: str,
        crew_template: str,
        workflow: str = "implement",
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

    def _require_provisioner(self) -> CrewPodProvisioner:
        if self._provisioner is None:
            raise RuntimeError(
                "no crew-pod provisioner configured — the worker constructs"
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
            self.provision_crew_pod,
            self.destroy_crew_pods,
            self.fulfill_crew,
        ]
