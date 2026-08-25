"""Governance, against policy compiled from
`examples/hello-saas.yaml` and *installed* on the live cluster.

Two layers. The OPA half queries the running OPA's decision paths directly
(port-forward), so what is asserted is the bundle the operator actually
served — per-role scoping, the blueprint's branch and path policies, run
binding, cross-company tenancy, the platform scope. The gateway half drives
real calls with projected workload tokens through the whole pipeline:
authn -> registry enrichment -> compiled decision -> audit.

The roster is lean: the PM is the only standing hire and execution is
ephemeral crews, so the executor-side checks here use the ephemeral input
shape (role + crew_template + crew_role), and a standing backend-engineer
input finds nothing in the compiled bundle. Injection deny + recovery runs in
test_injection.py through the crew executor's deliver dispatch.
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
    psql_json,
)

COMPANY_DECISION = "poseidon/authz/hello_saas/decision"
PLATFORM_DECISION = "poseidon/platform/decision"


def _decide(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(f"{base}/v1/data/{path}", json={"input": payload}, timeout=10)
    assert resp.status_code == 200, resp.text
    result = resp.json().get("result")
    assert result is not None, f"no decision document at {path}"
    return result


def standing(
    role: str,
    tool: str,
    *,
    verified: bool = True,
    args: dict | None = None,
    company: str = "hello-saas",
) -> dict[str, Any]:
    return {
        "company": company,
        "agent_id": f"{role}-1",
        "lifecycle": "standing",
        "parent_agent_id": "",
        "role": role,
        "run_id": "run-gov",
        "workflow": "implement",
        "tool": tool,
        "args": args or {},
        "run_binding_verified": verified,
    }


def executor(
    tool: str,
    *,
    verified: bool = True,
    args: dict | None = None,
    company: str = "hello-saas",
) -> dict[str, Any]:
    """The crew executor's input shape — the only execution identity
    there is no standing backend-engineer role."""
    return {
        "company": company,
        "agent_id": "feature-delivery-executor-run-gov",
        "lifecycle": "ephemeral",
        "parent_agent_id": "pm-1",
        "role": "backend-engineer",
        "crew_template": "feature-delivery",
        "crew_role": "executor",
        "run_id": "run-gov",
        "workflow": "implement",
        "tool": tool,
        "args": args or {},
        "run_binding_verified": verified,
    }


# -- the installed bundle decides (OPA layer) --------------------------------


def test_installed_bundle_enforces_the_blueprint(stack: None) -> None:
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        # Per-role scoping: the crew executor works inside a verified run
        # binding; without one the same call fails closed (wrong/stale
        # run_id — the data.runs.assignments successor).
        allowed = _decide(base, COMPANY_DECISION, executor("sandbox.exec"))
        assert allowed["allow"] is True

        unbound = _decide(
            base, COMPANY_DECISION, executor("sandbox.exec", verified=False)
        )
        assert unbound == {"allow": False, "rule": "default_deny"}

        # Provable in policy: a standing
        # backend-engineer finds nothing in the compiled bundle — the role
        # exists only as crew members.
        fired = _decide(
            base, COMPANY_DECISION, standing("backend-engineer", "sandbox.exec")
        )
        assert fired == {"allow": False, "rule": "default_deny"}

        # The PM cannot call executor tools — a rule, not a prompt.
        for tool in ("sandbox.exec", "sandbox.write_file", "git.open_pr"):
            denied = _decide(
                base,
                COMPANY_DECISION,
                standing("pm", tool, args={"branch": "feature/x", "paths": []}),
            )
            assert denied["allow"] is False, tool

        # The blueprint's own policies, on real argument shapes.
        # `main` violates both branch rules; the lowest-sorting deny wins,
        # so pin the family, not which of the two fired.
        protected = _decide(
            base,
            COMPANY_DECISION,
            executor("git.open_pr", args={"branch": "main", "paths": ["a.py"]}),
        )
        assert protected["allow"] is False
        assert protected["rule"].startswith("git_open_pr_")

        ci_paths = _decide(
            base,
            COMPANY_DECISION,
            executor(
                "git.open_pr",
                args={"branch": "feature/x", "paths": [".github/workflows/ci.yml"]},
            ),
        )
        assert ci_paths == {"allow": False, "rule": "git_open_pr_denied_paths"}

        # Tenancy: another company's caller finds nothing in this package.
        alien = _decide(
            base, COMPANY_DECISION, executor("sandbox.exec", company="evil-corp")
        )
        assert alien == {"allow": False, "rule": "default_deny"}

        # The platform scope lives in its own package: merge is platform-only.
        merge = _decide(
            base,
            PLATFORM_DECISION,
            {
                "company": "",
                "agent_id": "orchestrator",
                "lifecycle": "platform",
                "parent_agent_id": "",
                "role": "platform",
                "run_id": "run-gov",
                "workflow": "",
                "tool": "git.merge_pr",
                "args": {},
                "run_binding_verified": False,
            },
        )
        assert merge["allow"] is True
        company_merge = _decide(base, COMPANY_DECISION, executor("git.merge_pr"))
        assert company_merge["allow"] is False


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


def test_installed_bundle_scopes_issue_review_and_crew_tools(stack: None) -> None:
    """Per-role scoping beyond the sandbox tools: PM owns issues + crew
    intent, the reviewer owns the review tools, nobody else does — and
    crew.request is narrowed to the conductor's may_spawn list."""
    with port_forward("svc/opa", 8181) as port:
        base = f"http://127.0.0.1:{port}"
        assert (
            _decide(base, COMPANY_DECISION, standing("pm", "issues.create"))["allow"]
            is True
        )
        assert (
            _decide(base, COMPANY_DECISION, executor("issues.create"))["allow"] is False
        )

        ok = _decide(
            base,
            COMPANY_DECISION,
            standing(
                "pm",
                "crew.request",
                args={"crew_template": "feature-delivery", "reason": "r"},
            ),
        )
        assert ok["allow"] is True
        outside = _decide(
            base,
            COMPANY_DECISION,
            standing("pm", "crew.request", args={"crew_template": "shadow-ops"}),
        )
        assert outside["allow"] is False

        reviewer = {
            "company": "hello-saas",
            "agent_id": "reviewer-01X",
            "lifecycle": "ephemeral",
            "parent_agent_id": "pm-1",
            "role": "sdet",
            "crew_template": "feature-review",
            "crew_role": "reviewer",
            "run_id": "run-gov",
            "workflow": "review",
            "tool": "git.read",
            "args": {},
            "run_binding_verified": True,
        }
        assert _decide(base, COMPANY_DECISION, reviewer)["allow"] is True
        assert (
            _decide(base, COMPANY_DECISION, {**reviewer, "tool": "git.merge_pr"})["allow"]
            is False
        )


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

    rows = psql_json(
        "SELECT COALESCE(json_agg(c), '[]'::json) FROM ("
        "SELECT * FROM registry.crew_requests WHERE run_id = :'run_id'"
        ") c",
        run_id=run_id,
    )
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
