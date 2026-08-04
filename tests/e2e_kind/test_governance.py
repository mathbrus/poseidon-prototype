"""The step-12 governance regression, against policy compiled from
`examples/hello-saas.yaml` and *installed* on the live cluster.

Two layers. The OPA half queries the running OPA's decision paths directly
(port-forward), so what is asserted is the bundle the operator actually
served — per-role scoping, the blueprint's branch and path policies, run
binding, cross-company tenancy, the platform scope. The gateway half drives
real calls with projected workload tokens through the whole pipeline:
authn -> registry enrichment -> compiled decision -> audit.

E2-class injection deny + recovery runs in test_injection.py through the
transitional standing engineer (sequencing choice 6) — that suite now runs
against compiled policy simply because nothing else exists. The same
checks re-run against the crew executor at plan step 19.
"""

import asyncio
import subprocess
import time
from typing import Any

import httpx

from .conftest import COMPANY_NS, PLATFORM_NS
from .test_operator_lifecycle import port_forward
from .test_registry_durability import projected_token

OPA_LOCAL_PORT = 18182
COMPANY_DECISION = "poseidon/authz/hello_saas/decision"
PLATFORM_DECISION = "poseidon/platform/decision"


def _decide(base: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    resp = httpx.post(f"{base}/v1/data/{path}", json={"input": payload}, timeout=10)
    assert resp.status_code == 200, resp.text
    result = resp.json().get("result")
    assert result is not None, f"no decision document at {path}"
    return result


def standing(role: str, tool: str, *, verified: bool = True, args: dict | None = None,
             company: str = "hello-saas") -> dict[str, Any]:
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


# -- the installed bundle decides (OPA layer) --------------------------------


def test_installed_bundle_enforces_the_blueprint(stack: None) -> None:
    with port_forward("opa", OPA_LOCAL_PORT, 8181) as base:
        # Per-role scoping: the engineer works inside a verified run
        # binding; without one the same call fails closed (wrong/stale
        # run_id — the data.runs.assignments successor).
        allowed = _decide(base, COMPANY_DECISION,
                          standing("backend-engineer", "sandbox.exec"))
        assert allowed["allow"] is True

        unbound = _decide(base, COMPANY_DECISION,
                          standing("backend-engineer", "sandbox.exec", verified=False))
        assert unbound == {"allow": False, "rule": "default_deny"}

        # §4.5 exit: the PM cannot call executor tools — a rule, not a prompt.
        for tool in ("sandbox.exec", "sandbox.write_file", "git.open_pr"):
            denied = _decide(base, COMPANY_DECISION,
                             standing("pm", tool, args={"branch": "feature/x",
                                                        "paths": []}))
            assert denied["allow"] is False, tool

        # The blueprint's own policies, on real argument shapes.
        # `main` violates both branch rules; the lowest-sorting deny wins,
        # so pin the family, not which of the two fired.
        protected = _decide(base, COMPANY_DECISION,
                            standing("backend-engineer", "git.open_pr",
                                     args={"branch": "main", "paths": ["a.py"]}))
        assert protected["allow"] is False
        assert protected["rule"].startswith("git_open_pr_")

        ci_paths = _decide(base, COMPANY_DECISION,
                           standing("backend-engineer", "git.open_pr",
                                    args={"branch": "feature/x",
                                          "paths": [".github/workflows/ci.yml"]}))
        assert ci_paths == {"allow": False, "rule": "git_open_pr_denied_paths"}

        # Path ownership: tests/ belongs to engineer+sdet templates; any
        # other role touching it is denied by the ownership rule.
        foreign = _decide(base, COMPANY_DECISION,
                          standing("pm", "git.open_pr",
                                   args={"branch": "feature/x",
                                         "paths": ["tests/test_x.py"]}))
        assert foreign["allow"] is False
        assert foreign["rule"].startswith("git_open_pr_path_ownership")

        # Tenancy: another company's caller finds nothing in this package.
        alien = _decide(base, COMPANY_DECISION,
                        standing("backend-engineer", "sandbox.exec",
                                 company="evil-corp"))
        assert alien == {"allow": False, "rule": "default_deny"}

        # The platform scope lives in its own package: merge is platform-only.
        merge = _decide(base, PLATFORM_DECISION, {
            "company": "", "agent_id": "orchestrator", "lifecycle": "platform",
            "parent_agent_id": "", "role": "platform", "run_id": "run-gov",
            "workflow": "", "tool": "git.merge_pr", "args": {},
            "run_binding_verified": False,
        })
        assert merge["allow"] is True
        company_merge = _decide(base, COMPANY_DECISION,
                                standing("backend-engineer", "git.merge_pr"))
        assert company_merge["allow"] is False


# -- the whole pipeline decides (gateway layer) ------------------------------


class _McpCaller:
    """One authenticated MCP session against the NodePort gateway."""

    def __init__(self, gateway_url: str, token: str) -> None:
        self._url = gateway_url
        self._token = token

    async def call(self, tool: str, args: dict[str, Any]):
        from orchestrator.clients import GatewayClient

        async with GatewayClient(self._url, self._token) as gateway:
            return await gateway._session.call_tool(tool, args)


def _call(env: dict, token: str, tool: str, args: dict[str, Any]):
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    return asyncio.run(_McpCaller(gateway_url, token).call(tool, args))


def test_pm_is_denied_executor_tools_at_the_gateway(stack: None, env: dict) -> None:
    """The PM instance's own workload identity, through the live pipeline:
    the compiled bundle leaves `pm` no sandbox surface at all."""
    token = projected_token(COMPANY_NS, "pm-1")
    result = _call(env, token, "sandbox.exec",
                   {"run_id": "run-gov-pm", "cmd": "id", "timeout_s": 5})
    assert result.isError
    assert "default_deny" in result.content[0].text


def test_run_binding_is_live_registry_state(stack: None, env: dict) -> None:
    """Wrong -> denied; registered -> allowed; retired -> denied again.
    The binding is the registration row, resolved fresh on every call."""
    from orchestrator.clients import GatewayClient

    run_id = f"run-gov-{int(time.time())}"
    engineer = projected_token(COMPANY_NS, "backend-engineer-1")
    orchestrator = projected_token(PLATFORM_NS, "orchestrator-worker")
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")

    # Wrong/unknown run id: nothing registered -> fail closed.
    before = _call(env, engineer, "sandbox.list_dir",
                   {"run_id": run_id, "path": "."})
    assert before.isError and "default_deny" in before.content[0].text

    async def admin(action: str) -> None:
        async with GatewayClient(gateway_url, orchestrator) as gateway:
            if action == "register":
                # A dead-but-fast address (the gateway's own localhost, a
                # closed port -> instant refusal): policy decides before
                # the backend is reached, which is all this needs.
                await gateway.register_sandbox(
                    run_id, "http://127.0.0.1:9", COMPANY_NS, "gov-fake"
                )
            else:
                await gateway.unregister_sandbox(run_id)

    asyncio.run(admin("register"))
    try:
        bound = _call(env, engineer, "sandbox.list_dir",
                      {"run_id": run_id, "path": "."})
        # Policy allowed (the binding verified); only the backend fails,
        # because the registered address answers nothing.
        assert bound.isError
        text = bound.content[0].text
        assert "default_deny" not in text and "denied by policy" not in text
    finally:
        asyncio.run(admin("unregister"))

    stale = _call(env, engineer, "sandbox.list_dir",
                  {"run_id": run_id, "path": "."})
    assert stale.isError and "default_deny" in stale.content[0].text


def test_denies_are_in_the_audit_story(stack: None) -> None:
    """The PM's denied sandbox.exec above must be reconstructable."""
    proc = subprocess.run(
        ["kubectl", "-n", PLATFORM_NS, "exec", "-i", "statefulset/postgres", "--",
         "psql", "-U", "poseidon", "-d", "poseidon", "-At", "-c",
         "SELECT count(*) FROM registry.audit_events"
         " WHERE agent_id = 'pm-1' AND decision = 'deny'"
         "   AND tool = 'sandbox.exec' AND ts > now() - interval '10 minutes'"],
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert int(proc.stdout.strip()) >= 1
