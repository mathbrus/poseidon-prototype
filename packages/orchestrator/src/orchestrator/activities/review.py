"""The review activities: thin clients, as always —
sequencing, the merge gate and its timers live in the workflow.

Retry safety, per activity:
    create_review_run   — upserts the run row; safe.
    review_config       — read-only; safe.
    read_pr             — read-only against GitHub via the gateway; safe.
    record_gate_outcome — appends an audit event; a retried append
                          duplicates a row, never loses one (the audit
                          store is append-only by design).
    merge_pr            — GitHub's merge endpoint is idempotent for an
                          already-merged PR (it answers 405, surfaced as a
                          backend error the workflow reports honestly).

The merge itself: `git.merge_pr` is a *gateway tool
call* under the orchestrator's platform identity — one of the two mediation
paths, audited at the gateway like any agent call — issued by the workflow
only after the human gate approved. The gate outcome events here are the
`process-gate` audit source: approvals are workflow state, so the
audit story is where a human can later see that the gate, not a bug,
released the merge.
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
from orchestrator.clients import GatewayClient
from orchestrator.config import WorkerSettings
from registry import AuditStore, Runs
from registry.bundles import AgentInstances, Companies


class ReviewActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances | None = None,
        audit: AuditStore | None = None,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._instances = instances
        self._audit = audit
        self._log = get_logger("orchestrator.review")

    @activity.defn(name="create_review_run")
    async def create_review_run(self, run_id: str, pr_number: int) -> None:
        from orchestrator.workflows.review import review_workflow_id

        await asyncio.to_thread(
            self._runs.start,
            run_id,
            f"review pull request #{pr_number}",
            "review",
            review_workflow_id(run_id),
            self._settings.company,
        )
        self._log.bind(run_id=run_id).info(
            "review run created", extra={"pr_number": pr_number}
        )

    @activity.defn(name="review_config")
    async def review_config(self) -> dict[str, Any]:
        """The review workflow's shape, from the applied blueprint:
        which crew reviews, which standing conductor's authority the crew
        request is recorded under, and which signal the merge gate parks
        on — gate config comes from the compiled plan, never hardcoded.
        Fail-closed, like implement_config."""
        def read() -> dict[str, Any]:
            spec = self._companies.get(self._settings.company) or {}
            config = next(
                (w for w in spec.get("workflows") or []
                 if w.get("name") == "review"),
                None,
            )
            if config is None or not config.get("enabled", False):
                raise RuntimeError(
                    f"company {self._settings.company!r} has no enabled"
                    " review workflow in its applied blueprint"
                )
            crew_template = config.get("crew")
            if not crew_template:
                raise RuntimeError(
                    "review workflow binds no crew — the reviewer is an"
                    " ephemeral crew member; the compiler should"
                    " have refused this blueprint"
                )
            conductor_hire = config["conductor"]
            instance = None
            if self._instances is not None:
                instance = next(
                    (i for i in self._instances.for_company(self._settings.company)
                     if i.get("hire") == conductor_hire),
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
                "merge_gate": (config.get("gates") or {}).get("merge", "none"),
            }

        return await asyncio.to_thread(read)

    @activity.defn(name="read_pr")
    async def read_pr(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """PR metadata under the platform identity: the head ref is what
        the review sandbox is provisioned from, the title and URL
        seed the reviewer's task text and the run record."""
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            pr = await gateway.read_pr(
                run_id, self._settings.target_repo, pr_number
            )
        if not pr.get("head"):
            raise RuntimeError(
                f"pull request #{pr_number} has no head ref — nothing to"
                " provision a review sandbox from"
            )
        # The reviewer reads the diff itself (git.read is in its scope);
        # the workflow only needs the shape, not the patches.
        return {k: v for k, v in pr.items() if k != "files"}

    @activity.defn(name="record_gate_outcome")
    async def record_gate_outcome(
        self, run_id: str, gate: str, kind: str, workflow_id: str
    ) -> None:
        """One `source=process-gate` audit event: parked gates,
        escalations and verdicts become first-class rows in the run's
        story, carrying the workflow id that maps back to run_id."""
        if self._audit is None:
            raise RuntimeError("review activities constructed without an audit store")
        payload = {"gate": gate, "outcome": kind}
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            agent_id="orchestrator",
            role="platform",
            tool=f"gate.{gate}",
            args_digest=hashlib.sha256(
                json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
            ).hexdigest(),
            decision="allow" if kind == "approved" else "deny",
            rule=f"gate_{kind}",
            latency_ms=0,
            backend_status=None,
            source="process-gate",
            company=self._settings.company,
            lifecycle="platform",
            parent_agent_id="",
            workflow=workflow_id,
        )
        await asyncio.to_thread(self._audit.append, event)
        self._log.bind(run_id=run_id).info(
            "gate outcome recorded", extra={"gate": gate, "outcome": kind}
        )

    @activity.defn(name="merge_pr")
    async def merge_pr(self, run_id: str, pr_number: int) -> dict[str, Any]:
        """The post-gate merge: a gateway tool call under the
        orchestrator's platform identity — audited at the gateway — that
        the workflow issues strictly after the merge gate approved."""
        started = time.monotonic()
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            result = await gateway.merge_pr(
                run_id, self._settings.target_repo, pr_number
            )
        self._log.bind(run_id=run_id).info(
            "pr merged",
            extra={"pr_number": pr_number, "merged": result.get("merged"),
                   "latency_ms": int((time.monotonic() - started) * 1000)},
        )
        if not result.get("merged"):
            raise RuntimeError(f"merge of pull request #{pr_number} did not land")
        return result

    def all(self) -> list[Any]:
        return [
            self.create_review_run,
            self.review_config,
            self.read_pr,
            self.record_gate_outcome,
            self.merge_pr,
        ]
