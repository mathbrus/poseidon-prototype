"""Crew fulfillment activities: identity minting and pod provisioning.

Mint writes the `run_participants` binding row (the fulfill half of the
ephemeral-identity contract; the gateways own the verify half)
and signs the short-TTL platform JWT; revoke terminates rows — the write
that kills the identity's tokens before their TTL.

Provision creates the ephemeral executor's brain-host pod through the
bounded `CrewPodProvisioner` — the minted token is injected into the pod
env *here*, at provision time, so it never rides workflow history — and
destroy deletes by the run+kind label selector.

Everything emits `process-lifecycle` audit events onto the step-13
ingestion contract, so a run's story shows who was minted, provisioned,
revoked and destroyed, when, under whose authority. Step 16d composes
these into the fulfill orchestration; nothing here dispatches work.
"""

import asyncio
import hashlib
import json
import time
from datetime import datetime, timezone
from typing import Any

from temporalio import activity

import httpx

from common.audit import AuditEvent
from common.telemetry import get_logger
from orchestrator.clients import AgentClient
from orchestrator.config import WorkerSettings
from orchestrator.crewpods import CrewPodProvisioner
from orchestrator.identity import ephemeral_agent_id, mint_ephemeral
from registry import AuditStore, RunParticipants
from registry.crews import request_id


def _digest(payload: dict[str, Any]) -> str:
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()


class CrewActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        participants: RunParticipants,
        audit: AuditStore,
        provisioner: CrewPodProvisioner | None = None,
        crew_requests: Any = None,
        companies: Any = None,
        ledger: Any = None,
        runs: Any = None,
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

    @activity.defn(name="mint_participant")
    async def mint_participant(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Bind one crew member to one run and sign its identity.

        spec: {run_id, company, crew_template, member, role_template,
        parent_agent_id}. Retry-safe: the agent_id derives from
        {crew, member, run}, so a re-mint lands on the same row and signs a
        fresh token — one identity, never two. A re-mint after the run's
        teardown swept the row raises `ParticipantRevoked`: revocation is
        final.
        """
        started = time.monotonic()
        agent_id = ephemeral_agent_id(
            spec["crew_template"], spec["member"], spec["run_id"]
        )
        minted = mint_ephemeral(
            self._require_key(),
            agent_id=agent_id,
            company=spec["company"],
            role_template=spec["role_template"],
            run_id=spec["run_id"],
            parent_agent_id=spec["parent_agent_id"],
            ttl_s=self._settings.ephemeral_ttl_s,
        )
        await asyncio.to_thread(
            self._participants.add,
            agent_id,
            spec["run_id"],
            spec["parent_agent_id"],
            spec["crew_template"],
            spec["member"],
        )
        await self._emit(
            run_id=spec["run_id"],
            agent_id=agent_id,
            role=spec["role_template"],
            company=spec["company"],
            parent_agent_id=spec["parent_agent_id"],
            tool="lifecycle.mint",
            rule="participant_minted",
            digest=_digest({k: v for k, v in spec.items()}),
            started=started,
        )
        self._log.bind(run_id=spec["run_id"]).info(
            "participant minted", extra={"agent_id": agent_id}
        )
        return {
            "agent_id": agent_id,
            "token": minted.token,
            "ttl_s": minted.ttl_s,
        }

    @activity.defn(name="revoke_participants")
    async def revoke_participants(self, run_id: str) -> list[str]:
        """Terminate every live participant of the run — the terminal-path
        sweep. From these writes on, the gateways find no live rows
        and every minted token of the run is dead, TTL notwithstanding."""
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
                digest=_digest({"run_id": run_id}),
                started=started,
            )
        self._log.bind(run_id=run_id).info(
            "participants revoked", extra={"agent_ids": swept}
        )
        return swept

    @activity.defn(name="provision_crew_pod")
    async def provision_crew_pod(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Create the crew member's brain-host pod, minting a fresh
        token into its env — provision-time injection is what keeps the
        credential out of workflow history.

        spec: {run_id, company, crew_template, member, role_template,
        parent_agent_id}. The participants row must already exist (the
        mint activity); re-asserting it here lets a retry after a crash
        converge on the same identity. A retry that lost the race with its
        run's teardown raises `ParticipantRevoked` instead — revocation is
        final, and a pod created past that point would hold
        a revoked identity with nothing left to collect it.
        """
        started = time.monotonic()
        provisioner = self._require_provisioner()
        agent_id = ephemeral_agent_id(
            spec["crew_template"], spec["member"], spec["run_id"]
        )
        minted = mint_ephemeral(
            self._require_key(),
            agent_id=agent_id,
            company=spec["company"],
            role_template=spec["role_template"],
            run_id=spec["run_id"],
            parent_agent_id=spec["parent_agent_id"],
            ttl_s=self._settings.ephemeral_ttl_s,
        )
        await asyncio.to_thread(
            self._participants.add,
            agent_id,
            spec["run_id"],
            spec["parent_agent_id"],
            spec["crew_template"],
            spec["member"],
        )
        platform = self._settings.platform_namespace
        env = {
            "MCP_GATEWAY_URL":
                f"http://mcp-gateway.{platform}.svc.cluster.local:8080",
            "LLM_PROXY_URL":
                f"http://llm-gateway.{platform}.svc.cluster.local:4000",
            "MODEL": self._settings.model,
            "TARGET_REPO": self._settings.target_repo,
            "MAX_ITERATIONS": str(self._settings.max_iterations),
            "GATEWAY_TOKEN": minted.token,
        }
        # The step-23 race guard. `add` above refuses to revive a revoked
        # row, which stops a retry that begins after teardown's sweep; this
        # catches the rest — above all a retry still in flight while the
        # run ends, which is what leaked pods in practice.
        #
        # Two independent signals, because either alone has a hole: the
        # participant row (teardown revokes before it destroys, so a
        # non-active row means the sweep already passed) and the run's own
        # terminal state (the row can be *re-created* active by this very
        # activity when something deleted it meanwhile — the run record
        # cannot). A finalized run must never acquire a new pod.
        #
        # Handed to the provisioner rather than checked here, for two
        # reasons learned the hard way: checking *before* the call leaves
        # the remnant-deletion wait (measured at 31 s) between verdict and
        # creation, and checking *after* never runs at all — the workflow's
        # completion cancels this activity, and the cancellation lands on
        # the very next await.
        handle = await asyncio.to_thread(
            lambda: provisioner.provision(
                agent_id=agent_id,
                run_id=spec["run_id"],
                company=spec["company"],
                agent_image=self._settings.agent_image,
                env=env,
                still_wanted=lambda: self._collectable_now(
                    spec["run_id"], agent_id
                ),
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
            digest=_digest({k: v for k, v in spec.items()}),
            started=started,
        )
        self._log.bind(run_id=spec["run_id"]).info(
            "crew pod provisioned", extra={"agent_id": agent_id, "url": handle.url}
        )
        return {"agent_id": agent_id, "pod_name": handle.pod_name, "url": handle.url}

    def _collectable_now(self, run_id: str, agent_id: str) -> bool:
        """Whether anything provisioned for this run would still be torn
        down by someone. False once the run's teardown has swept past —
        seen either as a revoked binding row or as a finalized run.

        Synchronous by design: the provisioner calls it from the worker
        thread, immediately before creating the pod, so the answer cannot
        go stale between verdict and creation."""
        if self._participants.active(agent_id) is None:
            return False
        if self._runs is None:
            return True
        record = self._runs.get(run_id)
        # A missing run row is not evidence of finalization (the e2e
        # fixtures delete rows); only an explicitly terminal one is.
        return record is None or record.status not in ("done", "failed")

    @activity.defn(name="destroy_crew_pods")
    async def destroy_crew_pods(self, run_id: str) -> list[str]:
        """Delete the run's crew pods — by the {run_id, kind} label
        selector only (the 16c bound). Idempotent; audited per pod. With no
        provisioner configured nothing could ever have been provisioned, so
        compensation is honestly a no-op."""
        if self._provisioner is None:
            return []
        started = time.monotonic()
        provisioner = self._require_provisioner()
        deleted = await asyncio.to_thread(provisioner.destroy, run_id)
        for name in deleted:
            await self._emit(
                run_id=run_id,
                agent_id=name,
                role="",
                company="",
                parent_agent_id="",
                tool="lifecycle.destroy_pod",
                rule="crew_pod_destroyed",
                digest=_digest({"run_id": run_id}),
                started=started,
            )
        return deleted

    @activity.defn(name="fulfill_crew")
    async def fulfill_crew(self, spec: dict[str, Any]) -> dict[str, Any]:
        """The fulfillment composition: consume the recorded request ->
        mint every member -> pod for the acting member (token injected at
        provision) -> dispatch the planner on that brain host under its own
        identity -> hand back the brief for the executor dispatch. The
        orchestrator-fixed graph IS the planner->executor handoff; there is
        no free-form crew chat.

        spec: {run_id, company, crew_template, conductor, task_text,
        workflow?}. `workflow` names the calling workflow in the recorded
        decision input (implement by default, or review).
        Retry-safe end to end: consume is idempotent for this run, minting
        revives deterministic identities, provisioning replaces remnants.
        """
        run_id = spec["run_id"]
        crew_template = spec["crew_template"]
        conductor = spec["conductor"]
        started = time.monotonic()

        rid = request_id(conductor, crew_template, run_id)
        consumed = await asyncio.to_thread(
            self._crew_requests.consume, rid, run_id
        )
        if consumed is None and spec.get("record_under_authority"):
            # The workflow path: record the request under the
            # conductor's authority — same row, same policy. "Same policy"
            # is literal: the recording is allowed only if the compiled
            # decision the crew.request *tool* faces allows it, evaluated
            # against the same bundle with the same enriched input.
            await self._authorize_recording(
                run_id, spec["company"], conductor,
                spec.get("conductor_role", "pm"), crew_template,
                spec.get("workflow", "implement"),
            )
            await asyncio.to_thread(
                self._crew_requests.record,
                conductor,
                crew_template,
                run_id,
                spec.get("reason", "workflow-recorded under conductor authority"),
            )
            consumed = await asyncio.to_thread(
                self._crew_requests.consume, rid, run_id
            )
        if consumed is None:
            # No recorded, unexpired intent for this run: fulfillment does
            # not improvise authority (expired requests orphan nothing).
            raise RuntimeError(
                f"no recorded crew request for {conductor}/{crew_template}"
                f" on run {run_id} — record intent before fulfilling"
            )

        blueprint = await asyncio.to_thread(
            self._companies.get, spec["company"]
        ) or {}
        members = [
            m for m in blueprint.get("crewMembers") or []
            if m.get("crew") == crew_template
        ]
        if not members:
            raise RuntimeError(
                f"applied blueprint defines no members for crew {crew_template!r}"
            )

        minted: dict[str, dict[str, Any]] = {}
        pod: dict[str, Any] | None = None
        pod_member: dict[str, Any] | None = None
        for member in members:
            member_spec = {
                "run_id": run_id,
                "company": spec["company"],
                "crew_template": crew_template,
                "member": member["member"],
                "role_template": member["role_template"],
                "parent_agent_id": conductor,
            }
            minted[member["member"]] = await self.mint_participant(member_spec)
            if member.get("needs_sandbox") and pod is None:
                pod = await self.provision_crew_pod(member_spec)
                pod_member = member
        if pod is None or pod_member is None:
            raise RuntimeError(
                f"crew {crew_template!r} has no pod-hosting member"
            )

        # The planner (a no-pod member) borrows the acting member's
        # brain host, under its own identity: its gateway calls carry the
        # planner's minted token, so its compiled read-only scope binds.
        brief = spec["task_text"]
        planner = next(
            (m for m in members if not m.get("needs_sandbox")), None
        )
        if planner is not None:
            plan_run = await self._dispatch_plan(
                pod["url"], run_id, spec["task_text"],
                minted[planner["member"]]["token"],
            )
            if plan_run.get("error"):
                raise RuntimeError(f"planner failed: {plan_run['error']}")
            brief = plan_run.get("brief") or spec["task_text"]

        await self._emit(
            run_id=run_id,
            agent_id=pod["agent_id"],
            role=pod_member["role_template"],
            company=spec["company"],
            parent_agent_id=conductor,
            tool="lifecycle.fulfill",
            rule="crew_fulfilled",
            digest=_digest({"crew_template": crew_template,
                            "request_id": rid}),
            started=started,
        )
        return {
            "request_id": rid,
            "brief": brief,
            "executor_agent_id": pod["agent_id"],
            "executor_url": pod["url"],
            "members": sorted(minted),
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
            spec = (self._companies.get(company) or {})
            cap = float(((spec.get("budgets") or {}).get("per_run_usd")) or 0)
            headroom = True
            if cap > 0 and self._ledger is not None:
                headroom = self._ledger.spent_for_run(run_id) < cap
            return spawns, headroom

        spawns, headroom = await asyncio.to_thread(enrich)
        slug = company.replace(".", "_").replace("-", "_").replace("/", "_")
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
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                f"{self._settings.opa_url.rstrip('/')}"
                f"/v1/data/poseidon/authz/{slug}/decision",
                json={"input": decision_input},
            )
            resp.raise_for_status()
            result = resp.json().get("result") or {
                "allow": False, "rule": "no_policy_installed",
            }
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
            digest=_digest({"crew_template": crew_template}),
            started=started,
            decision="allow" if allowed else "deny",
            lifecycle="standing",
        )
        if not allowed:
            raise RuntimeError(
                f"crew request refused by policy rule"
                f" {result.get('rule')!r} for {conductor}/{crew_template}"
            )

    async def _dispatch_plan(
        self, url: str, run_id: str, task_text: str, token: str
    ) -> dict[str, Any]:
        async with AgentClient(url) as agent:
            await agent.dispatch(run_id, task_text, mode="plan",
                                 gateway_token=token)
            while True:
                if activity.in_activity():
                    activity.heartbeat()
                status = await agent.status(run_id)
                if status["status"] != "running":
                    return status
                await asyncio.sleep(self._settings.poll_interval_s)

    def _require_provisioner(self) -> CrewPodProvisioner:
        if self._provisioner is None:
            raise RuntimeError(
                "no crew-pod provisioner configured — the worker constructs"
                " one for the company namespace"
            )
        return self._provisioner

    def _require_key(self) -> str:
        key = self._settings.platform_jwt_key
        if not key:
            raise RuntimeError(
                "PLATFORM_JWT_KEY is not configured — the orchestrator cannot"
                " mint ephemeral identities without the platform signing key"
            )
        return key

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
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            agent_id=agent_id,
            role=role,
            tool=tool,
            args_digest=digest,
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status="ok",
            source="process-lifecycle",
            company=company,
            lifecycle=lifecycle,
            parent_agent_id=parent_agent_id,
            workflow="",
        )
        await asyncio.to_thread(self._audit.append, event)
        self._log.info("audit", extra=event.model_dump())

    def all(self) -> list[Any]:
        return [
            self.mint_participant,
            self.revoke_participants,
            self.provision_crew_pod,
            self.destroy_crew_pods,
            self.fulfill_crew,
        ]
