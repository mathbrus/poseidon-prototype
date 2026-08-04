"""OPA client: decision queries against operator-installed compiled bundles.

The decision path is per caller since plan step 12: company callers query
their company's package (`poseidon/authz/<slug>/decision`), the platform
lifecycle queries `poseidon/platform/decision`. Nothing is ever pushed into
OPA — the PoC's `data.runs.assignments` successor is the run-binding
enrichment the gateway injects into the decision *input* (§2b).
"""

from typing import Any

import httpx
from pydantic import BaseModel

PLATFORM_DECISION_PATH = "poseidon/platform/decision"


def company_decision_path(company: str) -> str:
    """Mirrors the compiler's `company_package` slug — the two must stay
    equal or every decision for the company lands on an empty document."""
    slug = company.replace(".", "_").replace("-", "_").replace("/", "_")
    return f"poseidon/authz/{slug}/decision"


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
