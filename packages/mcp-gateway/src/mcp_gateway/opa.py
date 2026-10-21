"""OPA client: decision queries against reconciler-installed compiled bundles.

The decision path is per caller (`common.policy`): company callers query
their company's package, the platform lifecycle queries the platform
bundle. Nothing is ever pushed into OPA — run binding rides as enrichment
the gateway injects into the decision *input*.
"""

from typing import Any

import httpx
from pydantic import BaseModel


class Decision(BaseModel):
    allow: bool
    rule: str | None = None


class OpaClient:
    def __init__(self, opa_url: str, http_client: httpx.AsyncClient) -> None:
        self._base = opa_url.rstrip("/")
        self._http = http_client

    async def decide(self, policy_input: dict[str, Any], path: str) -> Decision:
        resp = await self._http.post(
            f"{self._base}/v1/data/{path}", json={"input": policy_input}
        )
        resp.raise_for_status()
        result = resp.json().get("result")
        if result is None:
            # No document at the path: the company's bundle is not (or no
            # longer) installed. Fail closed — a torn-down company's callers
            # find no rule to match, which is what revocation means.
            return Decision(allow=False, rule="no_policy_installed")
        return Decision.model_validate(result)
