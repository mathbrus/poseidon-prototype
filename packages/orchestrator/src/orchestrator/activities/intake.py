"""The intake activities: thin clients, as always —
sequencing, timeouts, retries and the gate slot live in the workflow.

Retry safety, per activity:
    create_intake_run — upserts the run row; safe.
    check_budget      — read-only; safe. The activity-boundary
                        headroom check: enforcement stays local at the LLM
                        gateway, but a run that would be born exhausted
                        fails here, before any dispatch.
    intake_gate_config— read-only (the applied blueprint's workflow gates).
    dispatch_pm       — the PM answers 409 while running (re-attach) and
                        restarts terminal runs on re-dispatch.
    finalize_intake   — record_artifacts + finish are upserts.
"""

import asyncio
from typing import Any

from temporalio import activity

from common.telemetry import get_logger
from orchestrator.clients import AgentClient
from orchestrator.config import WorkerSettings
from registry import BudgetLedger, Runs
from registry.bundles import Companies


def _heartbeat() -> None:
    if activity.in_activity():
        activity.heartbeat()


class IntakeActivities:
    def __init__(
        self,
        settings: WorkerSettings,
        runs: Runs,
        companies: Companies,
        ledger: BudgetLedger,
    ) -> None:
        self._settings = settings
        self._runs = runs
        self._companies = companies
        self._ledger = ledger
        self._log = get_logger("orchestrator.intake")

    @activity.defn(name="create_intake_run")
    async def create_intake_run(self, run_id: str, goal_text: str) -> None:
        from orchestrator.workflows.intake import intake_workflow_id

        await asyncio.to_thread(
            self._runs.start,
            run_id,
            goal_text,
            "intake",
            intake_workflow_id(run_id),
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

    @activity.defn(name="intake_gate_config")
    async def intake_gate_config(self) -> dict[str, str]:
        """The intake workflow's gate configuration from the applied
        blueprint — the gate slot reads config, never hardcodes."""
        spec = await asyncio.to_thread(self._companies.get, self._settings.company) or {}
        for workflow in spec.get("workflows") or []:
            if workflow.get("name") == "intake":
                return dict(workflow.get("gates") or {})
        return {}

    @activity.defn(name="dispatch_pm")
    async def dispatch_pm(self, run_id: str, goal_text: str) -> dict[str, Any]:
        """Dispatch the standing PM to scope the goal and poll to terminal —
        the PM files tickets and records the crew request itself, through
        governed tools (steps 15/17)."""
        if not self._settings.pm_url:
            raise RuntimeError("PM_URL is not configured on the worker")
        async with AgentClient(self._settings.pm_url) as agent:
            await agent.dispatch(run_id, goal_text)
            while True:
                _heartbeat()
                status = await agent.status(run_id)
                if status["status"] != "running":
                    self._log.bind(run_id=run_id).info(
                        "pm finished", extra={"pm_status": status}
                    )
                    return status
                await asyncio.sleep(self._settings.poll_interval_s)

    @activity.defn(name="finalize_intake")
    async def finalize_intake(
        self, run_id: str, result: dict[str, Any] | None, error: str | None
    ) -> dict[str, Any]:
        """Link the tickets to the run record and write the honest terminal
        state ('run record links ticket URLs')."""
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
            self.intake_gate_config,
            self.dispatch_pm,
            self.finalize_intake,
        ]
