"""The intake activities: thin clients, as always —
sequencing, timeouts, retries and the gate slot live in the workflow.

Retry safety, per activity:
    create_intake_run — upserts the run row; safe.
    check_budget      — read-only; safe. The activity-boundary
                        headroom check: enforcement stays local at the LLM
                        gateway, but a run that would be born exhausted
                        fails here, before any dispatch.
    intake_config     — read-only (the applied blueprint's intake block
                        and the conductor's address).
    finalize_intake   — record_artifacts + finish are upserts.
"""

import asyncio
from typing import Any

from temporalio import activity

from common.telemetry import get_logger
from orchestrator.activities.blueprint import workflow_config
from orchestrator.config import WorkerSettings
from orchestrator.workflows.base import workflow_id
from registry import AgentInstances, BudgetLedger, Companies, Runs


class IntakeActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        instances: AgentInstances,
        ledger: BudgetLedger,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._instances = instances
        self._ledger = ledger
        self._log = get_logger("orchestrator.intake")

    @activity.defn(name="create_intake_run")
    async def create_intake_run(self, run_id: str, goal_text: str) -> None:
        await asyncio.to_thread(
            self._runs.start,
            run_id,
            goal_text,
            "intake",
            workflow_id("intake", run_id),
            self._settings.company,
        )
        self._log.bind(run_id=run_id).info("intake run created")

    @activity.defn(name="check_budget")
    async def check_budget(self, run_id: str) -> dict[str, Any]:
        """Budget reservation at the run's birth: verify the run has
        headroom under the applied blueprint's per-run cap before anything
        is dispatched. Enforcement stays local at the LLM gateway —
        this is the activity-boundary observation of the same ledger."""

        def read() -> tuple[float, float]:
            spec = self._companies.get(self._settings.company) or {}
            cap = float(((spec.get("budgets") or {}).get("per_run_usd")) or 0)
            return cap, self._ledger.spent_for_run(run_id)

        cap, spent = await asyncio.to_thread(read)
        if cap > 0 and spent >= cap:
            raise RuntimeError(
                f"budget_exhausted: run {run_id} has no headroom"
                f" ({spent:.6f} of {cap:.6f} USD spent)"
            )
        return {"cap_usd": cap, "spent_usd": spent}

    @activity.defn(name="intake_config")
    async def intake_config(self) -> dict[str, Any]:
        """The intake workflow's shape from the applied blueprint: its gates
        (the gate slot reads config, never hardcodes) and the conductor's
        dispatch address. Fail-closed like the other config reads."""

        def read() -> dict[str, Any]:
            config, conductor = workflow_config(
                self._companies,
                self._instances,
                self._settings.company,
                "intake",
                crew_required=False,
            )
            return {
                "gates": dict(config.get("gates") or {}),
                "conductor_url": conductor["conductor_url"],
            }

        return await asyncio.to_thread(read)

    @activity.defn(name="finalize_intake")
    async def finalize_intake(
        self, run_id: str, result: dict[str, Any] | None, error: str | None
    ) -> dict[str, Any]:
        """Link the tickets to the run record and write the honest terminal
        state."""
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
            None,
            result.get("iterations", 0),
            error,
        )
        self._log.bind(run_id=run_id).info(
            "intake finalized", extra={"status": record.status, "tickets": ticket_urls}
        )
        return record.as_dict()

    def all(self) -> list[Any]:
        return [
            self.create_intake_run,
            self.check_budget,
            self.intake_config,
            self.finalize_intake,
        ]
