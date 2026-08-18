"""The conduct activities: the heartbeat's eyes, its authorization and its
audit trail. Thin clients, as always — sequencing and the tick cadence live
in the workflow.

Retry safety, per activity:
    conduct_config      — read-only; safe.
    conduct_observe     — read-only (registry + the open backlog under the
                          platform identity); safe. Only numbers, titles
                          and URLs leave here: issue bodies are untrusted
                          input the implement run reads itself.
    conduct_authorize   — evaluates the compiled conduct.start decision at
                          OPA and audits it; a re-attempt re-evaluates and
                          re-audits, which is the honest record of what was
                          asked. A deny is non-retryable by construction.
    record_conduct_tick — append-only audit write; a duplicate row is a
                          duplicate heartbeat line, never a wrong one.
"""

import asyncio
import hashlib
import json
import time
from datetime import UTC, datetime
from typing import Any

import httpx
from temporalio import activity
from temporalio.exceptions import ApplicationError

from common.audit import AuditEvent
from common.telemetry import get_logger
from orchestrator.clients import GatewayClient
from orchestrator.config import WorkerSettings
from registry import AuditStore, BudgetLedger, Runs
from registry.bundles import Companies


def _digest(payload: dict[str, Any]) -> str:
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


class ConductActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        ledger: BudgetLedger,
        audit: AuditStore,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._ledger = ledger
        self._audit = audit
        self._log = get_logger("orchestrator.conduct")

    def _conduct_run_id(self) -> str:
        from orchestrator.workflows.conduct import conduct_workflow_id

        return conduct_workflow_id(self._settings.company)

    @activity.defn(name="conduct_config")
    async def conduct_config(self) -> dict[str, Any] | None:
        """The applied blueprint's conduct section, or None — the workflow
        stops on None, which is what makes removal effective at the next
        tick regardless of what else still believes in the heartbeat."""
        spec = await asyncio.to_thread(self._companies.get, self._settings.company) or {}
        return spec.get("conduct") or None

    @activity.defn(name="conduct_observe")
    async def conduct_observe(self) -> dict[str, Any]:
        """What the tick decides from: in-flight runs and today's spend
        from the registry, and the oldest open issue no run has consumed —
        read under the platform identity, like every platform read."""

        def read() -> tuple[int, float, set[int]]:
            company = self._settings.company
            return (
                self._runs.active_count(company),
                self._ledger.spent_today(company),
                self._runs.started_issue_numbers(company),
            )

        active_runs, daily_spent, started = await asyncio.to_thread(read)
        async with GatewayClient(
            self._settings.mcp_gateway_url, self._settings.gateway_token()
        ) as gateway:
            issues = await gateway.read_open_issues(
                self._conduct_run_id(), self._settings.target_repo
            )
        candidates = sorted(
            (issue for issue in issues if int(issue["number"]) not in started),
            key=lambda issue: int(issue["number"]),
        )
        next_issue = None
        if candidates:
            first = candidates[0]
            next_issue = {
                "number": int(first["number"]),
                "title": str(first.get("title") or ""),
                "url": str(first.get("url") or ""),
            }
        observed = {
            "active_runs": active_runs,
            "daily_spent_usd": daily_spent,
            "open_candidates": len(candidates),
            "next_issue": next_issue,
        }
        self._log.info("conduct tick observed", extra=observed)
        return observed

    @activity.defn(name="conduct_authorize")
    async def conduct_authorize(
        self,
        child_run_id: str,
        workflow_name: str,
        active_runs: int,
        daily_spent_usd: float,
    ) -> None:
        """Evaluate the compiled conduct.start decision — the caps and the
        startable set bind here as policy, not as this module's opinion.
        The live counts ride the input (the active_spawns idiom); the
        decision is audited either way; a deny ends the tick."""
        company = self._settings.company
        slug = company.replace(".", "_").replace("-", "_").replace("/", "_")
        decision_input = {
            "company": company,
            "agent_id": "orchestrator",
            "lifecycle": "platform",
            "parent_agent_id": "",
            "role": "platform",
            "run_id": child_run_id,
            "workflow": "conduct",
            "tool": "conduct.start",
            "args": {
                "workflow": workflow_name,
                "active_runs": active_runs,
                "daily_spent_usd": daily_spent_usd,
            },
            "run_binding_verified": False,
        }
        started = time.monotonic()
        async with httpx.AsyncClient(timeout=10.0) as http:
            resp = await http.post(
                f"{self._settings.opa_url.rstrip('/')}"
                f"/v1/data/poseidon/authz/{slug}/decision",
                json={"input": decision_input},
            )
            resp.raise_for_status()
            result = resp.json().get("result") or {
                "allow": False,
                "rule": "no_policy_installed",
            }
        allowed = bool(result.get("allow"))
        await self._emit(
            run_id=child_run_id,
            tool="conduct.start",
            digest=_digest(decision_input["args"]),
            decision="allow" if allowed else "deny",
            rule=str(result.get("rule") or ""),
            latency_ms=int((time.monotonic() - started) * 1000),
        )
        if not allowed:
            raise ApplicationError(
                f"conduct.start refused by policy rule {result.get('rule')!r}"
                f" for workflow {workflow_name!r}",
                non_retryable=True,
            )

    @activity.defn(name="record_conduct_tick")
    async def record_conduct_tick(
        self, outcome: str, rule: str, workflow_id: str
    ) -> None:
        """Every tick leaves a row — started, idle and why, refused and
        why — under the heartbeat's own run id, so the loop's whole story
        is one correlated query. The decision column keeps its allow/deny
        contract (the gate-outcome idiom): a tick that started work is an
        allow; every other outcome declined or was refused to start."""
        await self._emit(
            run_id=self._conduct_run_id(),
            tool="conduct.tick",
            digest=_digest({"outcome": outcome, "rule": rule}),
            decision="allow" if outcome == "started" else "deny",
            rule=f"tick_{outcome}:{rule}",
            workflow=workflow_id,
        )
        self._log.info("conduct tick", extra={"outcome": outcome, "rule": rule})

    async def _emit(
        self,
        *,
        run_id: str,
        tool: str,
        digest: str,
        decision: str,
        rule: str,
        latency_ms: int = 0,
        workflow: str = "conduct",
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id,
            agent_id="orchestrator",
            role="platform",
            tool=tool,
            args_digest=digest,
            decision=decision,
            rule=rule,
            latency_ms=latency_ms,
            backend_status=None,
            source="process-lifecycle",
            company=self._settings.company,
            lifecycle="platform",
            parent_agent_id="",
            workflow=workflow,
        )
        await asyncio.to_thread(self._audit.append, event)

    def all(self) -> list[Any]:
        return [
            self.conduct_config,
            self.conduct_observe,
            self.conduct_authorize,
            self.record_conduct_tick,
        ]
