"""Crew fulfillment activities — the 16a and 16c slices.

16a: mint writes the `run_participants` binding row (the §2b contract's
fulfill half; the gateways' verify half has been live since steps 11/12)
and signs the short-TTL platform JWT; revoke terminates rows — the write
that kills the identity's tokens before their TTL.

16c: provision creates the ephemeral executor's brain-host pod through the
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

from common.audit import AuditEvent
from common.telemetry import get_logger
from orchestrator.config import WorkerSettings
from orchestrator.crewpods import CrewPodProvisioner
from orchestrator.identity import ephemeral_agent_id, mint_ephemeral
from registry import AuditStore, RunParticipants


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
    ) -> None:
        self._settings = settings
        self._participants = participants
        self._audit = audit
        self._provisioner = provisioner
        self._log = get_logger("orchestrator.crew")

    @activity.defn(name="mint_participant")
    async def mint_participant(self, spec: dict[str, Any]) -> dict[str, Any]:
        """Bind one crew member to one run and sign its identity.

        spec: {run_id, company, crew_template, member, role_template,
        parent_agent_id}. Retry-safe: the agent_id derives from
        {crew, member, run}, so a re-mint revives the same row and signs a
        fresh token — one identity, never two.
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
        sweep (§6.4). From these writes on, the gateways find no live rows
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
        """Create the crew member's brain-host pod (16c), minting a fresh
        token into its env — provision-time injection is what keeps the
        §2b credential out of workflow history.

        spec: {run_id, company, crew_template, member, role_template,
        parent_agent_id}. The participants row must already exist (16a's
        mint activity); re-minting here revives it, so a retry after a
        crash converges.
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
        handle = await asyncio.to_thread(
            lambda: provisioner.provision(
                agent_id=agent_id,
                run_id=spec["run_id"],
                company=spec["company"],
                agent_image=self._settings.agent_image,
                env=env,
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

    @activity.defn(name="destroy_crew_pods")
    async def destroy_crew_pods(self, run_id: str) -> list[str]:
        """Delete the run's crew pods — by the {run_id, kind} label
        selector only (the 16c bound). Idempotent; audited per pod."""
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
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            agent_id=agent_id,
            role=role,
            tool=tool,
            args_digest=digest,
            decision="allow",
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status="ok",
            source="process-lifecycle",
            company=company,
            lifecycle="ephemeral",
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
        ]
