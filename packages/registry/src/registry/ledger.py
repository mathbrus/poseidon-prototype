"""`budget_ledger` — written by the LLM gateway only.

One ledger with dimensions, never parallel accounts: every model
call lands here under {company, run_id, standing parent, ephemeral
participant, role, model}, so crew fan-out stays reconcilable and "what did
run X cost" is one SUM. The headroom read is what the hard-stop rides:
enforcement is local at the LLM gateway (no Temporal client there);
the workflow observes the stop through the brain's surfaced budget error.
"""

from typing import Any

from registry.db import Registry


class BudgetLedger:
    def __init__(self, registry: Registry) -> None:
        self._db = registry

    def record(
        self,
        *,
        company: str,
        run_id: str,
        agent_id: str,
        parent_agent_id: str,
        role: str,
        model: str,
        prompt_tokens: int,
        completion_tokens: int,
        usd: float,
    ) -> None:
        self._db.execute(
            """
            INSERT INTO registry.budget_ledger
                (company, run_id, agent_id, parent_agent_id, role, model,
                 prompt_tokens, completion_tokens, usd)
            VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                company,
                run_id,
                agent_id,
                parent_agent_id,
                role,
                model,
                prompt_tokens,
                completion_tokens,
                usd,
            ),
        )

    def spent_for_run(self, run_id: str) -> float:
        """Total attributed spend for one run — the per-run cap compares
        against this before every forwarded call."""
        rows = self._db.query(
            "SELECT COALESCE(SUM(usd), 0) AS spent"
            " FROM registry.budget_ledger WHERE run_id = %s",
            (run_id,),
        )
        return float(rows[0]["spent"])

    def entries_for_run(self, run_id: str) -> list[dict[str, Any]]:
        return self._db.query(
            "SELECT ts, company, run_id, agent_id, parent_agent_id, role,"
            " model, prompt_tokens, completion_tokens, usd"
            " FROM registry.budget_ledger WHERE run_id = %s ORDER BY ts, id",
            (run_id,),
        )
