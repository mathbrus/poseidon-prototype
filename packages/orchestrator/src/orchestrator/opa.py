"""Evaluating a compiled decision against the live OPA bundle.

The orchestrator queries OPA directly at two authorization chokepoints that
are not a gateway tool call: the workflow-recorded `crew.request` (crew
fulfilment under a conductor's authority) and the conduct heartbeat's
`conduct.start`. Both send the same decision-input shape the gateway sends
and read the same default-deny bundle — this module is the one place the
transport lives, so the two callers cannot drift on the endpoint, the
slug, or the no-policy-installed default. What each does with the verdict —
which audit event to emit, which error to raise — stays with the caller.
"""

from typing import Any

import httpx

from common.policy import company_decision_path

# A caller with no served bundle gets a deny, not an exception: an
# unconverged company must fail closed, exactly as the default-deny rule
# would inside a bundle that was installed.
NO_POLICY = {"allow": False, "rule": "no_policy_installed"}


async def evaluate_decision(
    opa_url: str, company: str, decision_input: dict[str, Any]
) -> dict[str, Any]:
    """POST one decision input to the company's decision entrypoint and
    return the `{allow, rule}` result, or the fail-closed default when OPA
    serves no policy for it."""
    async with httpx.AsyncClient(timeout=10.0) as http:
        resp = await http.post(
            f"{opa_url.rstrip('/')}/v1/data/{company_decision_path(company)}",
            json={"input": decision_input},
        )
        resp.raise_for_status()
        return resp.json().get("result") or dict(NO_POLICY)
