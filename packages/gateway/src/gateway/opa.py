"""OPA client: decision queries (contract C2) and run-assignment pushes.

Assignments live in OPA under data.runs.assignments as
{<agent_id>: [<run_id>, ...]}; the gateway is their source of truth and
replaces the whole document on every change.
"""

from typing import Any

import httpx
from pydantic import BaseModel

DECISION_PATH = "/v1/data/poseidon/poc/decision"
ASSIGNMENTS_PATH = "/v1/data/runs/assignments"


class Decision(BaseModel):
    allow: bool
    rule: str | None = None


class OpaClient:
    def __init__(self, opa_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = opa_url.rstrip("/")
        self._http = http_client
        self._assignments: dict[str, list[str]] = {}

    async def decide(self, policy_input: dict[str, Any]) -> Decision:
        resp = await self._http.post(self._base + DECISION_PATH, json={"input": policy_input})
        resp.raise_for_status()
        result = resp.json().get("result")
        if result is None:
            raise RuntimeError("OPA returned no decision (policy not loaded?)")
        return Decision.model_validate(result)

    async def assign_run(self, agent_id: str, run_id: str) -> None:
        runs = self._assignments.setdefault(agent_id, [])
        if run_id not in runs:
            runs.append(run_id)
        resp = await self._http.put(self._base + ASSIGNMENTS_PATH, json=self._assignments)
        resp.raise_for_status()
