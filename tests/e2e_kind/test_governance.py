"""Governance, against policy compiled from
`examples/hello-saas.yaml` and *installed* on the live cluster.

Two layers. The OPA half is a smoke pair against the running OPA's decision
path (port-forward): what it asserts is that the bundle the reconciler
actually served decides. The decision *matrix* — per-role scoping, branch
and path policies, one company's rules for its own callers only, crew
narrowing, merge automation — is pinned
per-decision against the real OPA in
packages/compiler/tests/test_bundle_decisions.py, and test_acceptance
proves the served revision is this compiler's output for this blueprint.
The gateway half drives real calls with projected workload tokens through
the whole pipeline: authn -> registry enrichment -> compiled decision ->
audit. Injection deny + recovery runs in test_injection.py through the
crew implementer's deliver dispatch.
"""

from typing import Any

import httpx

from common.policy import company_decision_path

from .cluster import (
    COMPANY,
    COMPANY_NS,
    PLATFORM_NS,
    db_now,
    events_since,
    mcp_call,
    port_forward,
    projected_token,
)

COMPANY_DECISION = company_decision_path(COMPANY)


def _decide(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(f"{base}/v1/data/{path}", json={"input": payload}, timeout=10)
    assert resp.status_code == 200, resp.text
    result = resp.json().get("result")
    assert result is not None, f"no decision document at {path}"
    return result


def implementer(
    tool: str,
    *,
    verified: bool = True,
    args: dict | None = None,
    company: str = COMPANY,
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
    one allow (the crew implementer inside a verified run binding) and one
    deny (the same call unbound, failing closed on the run-binding fact).
    The binding rides the request's input: OPA holds no pushed run data
    for the policy to read instead."""
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        pushed = httpx.get(f"{base}/v1/data/runs", timeout=10).json()
        assert not pushed.get("result"), f"OPA carries pushed run data: {pushed}"
        allowed = _decide(base, COMPANY_DECISION, implementer("sandbox.exec"))
        assert allowed["allow"] is True

        unbound = _decide(
            base, COMPANY_DECISION, implementer("sandbox.exec", verified=False)
        )
        assert unbound == {"allow": False, "rule": "default_deny"}


# -- the whole pipeline decides (gateway layer) ------------------------------


def test_pm_is_denied_implementer_tools_at_the_gateway(stack: None, env: dict) -> None:
    """The PM agent's own workload identity, through the live pipeline:
    the compiled bundle leaves `pm` no sandbox surface at all, and the
    refusal is on the record under its name."""
    token = projected_token(COMPANY_NS, "pm-1")
    since = db_now()
    result = mcp_call(
        env, token, "sandbox.exec", {"run_id": "run-gov-pm", "cmd": "id", "timeout_s": 5}
    )
    assert result.isError
    assert "default_deny" in result.content[0].text
    denies = [
        e
        for e in events_since(since)
        if e["agent_id"] == "pm-1"
        and e["tool"] == "sandbox.exec"
        and e["decision"] == "deny"
    ]
    assert [e["rule"] for e in denies] == ["default_deny"], denies


# -- the whole tool surface, live --------------------------------------------


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
        {"run_id": "run-gov", "repo": target, "number": 999999, "sha": "0" * 40},
    )
    assert allowed.isError  # backend: no such PR
    text = allowed.content[0].text
    assert "denied by policy" not in text
    assert "404" in text or "GitHub" in text

    # The one standing company identity left (the OPA layer already covers
    # the ephemeral implementer's merge deny).
    token = projected_token(COMPANY_NS, "pm-1")
    denied = mcp_call(
        env,
        token,
        "git.merge_pr",
        {"run_id": "run-gov", "repo": target, "number": 999999, "sha": "0" * 40},
    )
    assert denied.isError and "default_deny" in denied.content[0].text
