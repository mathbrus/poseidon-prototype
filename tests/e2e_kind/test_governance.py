"""Governance, against policy compiled from
`examples/hello-saas.yaml` and *installed* on the live cluster.

Two layers. The OPA half is a smoke pair against the running OPA's decision
path (port-forward): what it asserts is that the bundle the operator
actually served decides. The decision *matrix* — per-role scoping, branch
and path policies, tenancy, crew narrowing, merge delegation — is pinned
per-decision against the real OPA in
packages/compiler/tests/test_bundle_decisions.py, and test_acceptance
proves the served revision is this compiler's output for this blueprint.
The gateway half drives real calls with projected workload tokens through
the whole pipeline: authn -> registry enrichment -> compiled decision ->
audit. Injection deny + recovery runs in test_injection.py through the
crew executor's deliver dispatch.
"""

import json
import time
from typing import Any

import httpx

from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    mcp_call,
    port_forward,
    projected_token,
    psql,
)
from .conftest import crew_requests

COMPANY_DECISION = "poseidon/authz/hello_saas/decision"


def _decide(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(f"{base}/v1/data/{path}", json={"input": payload}, timeout=10)
    assert resp.status_code == 200, resp.text
    result = resp.json().get("result")
    assert result is not None, f"no decision document at {path}"
    return result


def executor(
    tool: str,
    *,
    verified: bool = True,
    args: dict | None = None,
    company: str = "hello-saas",
) -> dict[str, Any]:
    """The crew implementer's input shape — the only execution identity;
    there is no standing implementer."""
    return {
        "company": company,
        "agent_id": "feature-delivery-implementer-run-gov",
        "lifecycle": "ephemeral",
        "parent_agent_id": "pm-1",
        "role": "implementer",
        "crew_template": "feature-delivery",
        "run_id": "run-gov",
        "workflow": "implement",
        "tool": tool,
        "args": args or {},
        "run_binding_verified": verified,
    }


# -- the installed bundle decides (OPA layer) --------------------------------


def test_the_served_bundle_decides(stack: None) -> None:
    """The smoke pair: the served bundle answers with compiled decisions —
    one allow (the crew executor inside a verified run binding) and one
    deny (the same call unbound, failing closed on the run-binding fact)."""
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        allowed = _decide(base, COMPANY_DECISION, executor("sandbox.exec"))
        assert allowed["allow"] is True

        unbound = _decide(
            base, COMPANY_DECISION, executor("sandbox.exec", verified=False)
        )
        assert unbound == {"allow": False, "rule": "default_deny"}


# -- the whole pipeline decides (gateway layer) ------------------------------


def test_pm_is_denied_executor_tools_at_the_gateway(stack: None, env: dict) -> None:
    """The PM instance's own workload identity, through the live pipeline:
    the compiled bundle leaves `pm` no sandbox surface at all."""
    token = projected_token(COMPANY_NS, "pm-1")
    result = mcp_call(
        env, token, "sandbox.exec", {"run_id": "run-gov-pm", "cmd": "id", "timeout_s": 5}
    )
    assert result.isError
    assert "default_deny" in result.content[0].text


def test_denies_are_in_the_audit_story(stack: None) -> None:
    """The PM's denied sandbox.exec above must be reconstructable."""
    count = psql(
        "SELECT count(*) FROM registry.audit_events"
        " WHERE agent_id = :'agent' AND decision = 'deny'"
        "   AND tool = :'tool' AND ts > now() - interval '10 minutes'",
        agent="pm-1",
        tool="sandbox.exec",
    )
    assert int(count) >= 1


# -- the whole tool surface, live --------------------------------------------


def test_pm_records_crew_intent_at_the_gateway(stack: None, env: dict) -> None:
    """The one spawn model, tool path: the PM's projected
    identity records an idempotent, expiring crew_requests row — intent,
    never fulfillment."""
    token = projected_token(COMPANY_NS, "pm-1")
    run_id = f"run-gov-crew-{int(time.time())}"

    first = mcp_call(
        env,
        token,
        "crew.request",
        {
            "run_id": run_id,
            "crew_template": "feature-delivery",
            "reason": "governance e2e",
        },
    )
    assert not first.isError, first.content
    recorded = json.loads(first.content[0].text)
    assert recorded["status"] == "recorded"
    assert recorded["conductor"] == "pm-1"

    again = mcp_call(
        env,
        token,
        "crew.request",
        {
            "run_id": run_id,
            "crew_template": "feature-delivery",
            "reason": "governance e2e retried",
        },
    )
    assert json.loads(again.content[0].text)["id"] == recorded["id"]

    rows = crew_requests(run_id)
    assert len(rows) == 1 and rows[0]["status"] == "recorded"
    assert rows[0]["expires_at"] is not None

    denied = mcp_call(
        env,
        token,
        "crew.request",
        {"run_id": run_id, "crew_template": "shadow-ops", "reason": "hm"},
    )
    assert denied.isError and "default_deny" in denied.content[0].text


def test_merge_is_platform_only_at_the_gateway(stack: None, env: dict) -> None:
    """`git.merge_pr` provably restricted to the platform lifecycle: the
    orchestrator's call passes policy (the backend 404s on a PR that does
    not exist — nothing merged), while company roles are rule-denied."""
    target = env.get("TARGET_REPO", "")
    orchestrator = projected_token(PLATFORM_NS, "orchestrator-worker")
    allowed = mcp_call(
        env,
        orchestrator,
        "git.merge_pr",
        {"run_id": "run-gov", "repo": target, "number": 999999},
    )
    assert allowed.isError  # backend: no such PR
    text = allowed.content[0].text
    assert "denied by policy" not in text
    assert "404" in text or "GitHub" in text

    # The one standing company identity left (the OPA layer already covers
    # the ephemeral executor's merge deny).
    token = projected_token(COMPANY_NS, "pm-1")
    denied = mcp_call(
        env,
        token,
        "git.merge_pr",
        {"run_id": "run-gov", "repo": target, "number": 999999},
    )
    assert denied.isError and "default_deny" in denied.content[0].text
