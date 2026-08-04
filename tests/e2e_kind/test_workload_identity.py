"""Workload identity on kind (plan step 11 done-when).

Positive half: the in-cluster callers hold no static token anymore (their
env is checked structurally) and still authenticate — a token minted for
the orchestrator's ServiceAccount is accepted, and the standing agent's
own mounted projected token round-trips against the gateway from inside
its pod. The E1 suite passing on this cluster is the full-system proof;
these tests isolate the credential path.

Negative half: a token from the wrong service account, the wrong
namespace, or with the wrong audience is rejected *and audited* — the
audit store must show an `authn_*` deny for it.
"""

import subprocess
import time
from typing import Any

import httpx

from .conftest import COMPANY_NS, PLATFORM_NS, _psql_json

AUDIENCE = "poseidon-gateway"


def _kubectl(*args: str, check: bool = True) -> str:
    proc = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=60
    )
    if check:
        assert proc.returncode == 0, proc.stderr
    return proc.stdout.strip()


def mint_token(namespace: str, service_account: str, audience: str = AUDIENCE) -> str:
    """TokenRequest via kubectl: the same kind of projected token the
    kubelet mounts, minted for an arbitrary ServiceAccount."""
    return _kubectl(
        "-n", namespace, "create", "token", service_account,
        f"--audience={audience}", "--duration=10m",
    )


def gateway_status(env: dict[str, str], token: str | None) -> int:
    url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080") + "/mcp"
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(url, headers=headers, timeout=10).status_code


def authn_denies(rule: str, principal: str) -> list[dict[str, Any]]:
    """Recent authn deny events for one rule+principal, from the audit
    store (they carry no run_id — the credential never mapped to one).
    The rule name is a test-owned literal, inlined; the principal rides
    the conftest helper's psql variable binding."""
    return _psql_json(
        "SELECT COALESCE(json_agg(e ORDER BY e.ts), '[]'::json) FROM ("
        "SELECT * FROM registry.audit_events"
        f" WHERE decision = 'deny' AND rule = '{rule}'"
        "   AND agent_id = :'run_id'"
        "   AND ts > now() - interval '5 minutes'"
        ") e",
        principal,
        default="[]",
    )


def wait_for_authn_deny(rule: str, principal: str) -> list[dict[str, Any]]:
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        events = authn_denies(rule, principal)
        if events:
            return events
        time.sleep(1)
    raise AssertionError(f"no audited {rule} deny for {principal}")


# -- positive: projected tokens are the working credential ------------------


def test_no_static_gateway_credential_in_cluster_pods(stack: None) -> None:
    """The step-11 done-when ('all callers authenticate with projected
    tokens') held structurally: neither the worker nor the agent pod
    carries a bearer-token env var, so every green run on this cluster rode
    the projected tokens."""
    worker_env = _kubectl(
        "-n", PLATFORM_NS, "get", "deployment/orchestrator-worker",
        "-o", "jsonpath={.spec.template.spec.containers[0].env[*].name}",
    ).split()
    assert "ORCHESTRATOR_BEARER_TOKEN" not in worker_env
    assert "GATEWAY_TOKEN_FILE" in worker_env

    agent_env = _kubectl(
        "-n", COMPANY_NS, "get", "deployment/backend-engineer",
        "-o", "jsonpath={.spec.template.spec.containers[0].env[*].name}",
    ).split()
    assert "AGENT_BEARER_TOKEN" not in agent_env
    assert "GATEWAY_TOKEN_FILE" in agent_env


def test_orchestrator_projected_token_is_accepted(stack: None, env) -> None:
    token = mint_token(PLATFORM_NS, "orchestrator-worker")
    assert gateway_status(env, None) == 401  # control: no credential
    assert gateway_status(env, token) != 401


def test_agent_pod_token_round_trips_from_inside(stack: None) -> None:
    """The agent's actual mounted token, used from its own pod: proves the
    projection, the operator-created ServiceAccount, the registry mapping
    and the TokenReview path end to end."""
    pod = _kubectl(
        "-n", COMPANY_NS, "get", "pod",
        "-l", "app=backend-engineer", "--field-selector=status.phase=Running",
        "-o", "jsonpath={.items[0].metadata.name}",
    )
    probe = (
        "import urllib.request, urllib.error\n"
        "token = open('/var/run/poseidon/identity/token').read().strip()\n"
        "req = urllib.request.Request(\n"
        f"    'http://mcp-gateway.{PLATFORM_NS}.svc.cluster.local:8080/mcp',\n"
        "    headers={'Authorization': 'Bearer ' + token})\n"
        "try:\n"
        "    urllib.request.urlopen(req, timeout=10)\n"
        "    print(200)\n"
        "except urllib.error.HTTPError as e:\n"
        "    print(e.code)\n"
    )
    code = _kubectl("-n", COMPANY_NS, "exec", pod, "--", "python", "-c", probe)
    assert code != "401", "the agent's own projected token was rejected"


# -- negative: wrong principal, namespace, audience — rejected and audited --


def test_wrong_service_account_is_rejected_and_audited(stack: None, env) -> None:
    token = mint_token("default", "default")
    assert gateway_status(env, token) == 401
    events = wait_for_authn_deny(
        "authn_unknown_service_account", "system:serviceaccount:default:default"
    )
    assert all(e["decision"] == "deny" for e in events)


def test_wrong_namespace_same_name_is_rejected_and_audited(stack: None, env) -> None:
    """A ServiceAccount named like a real instance but living elsewhere
    verifies fine at the API server and must still map to nothing."""
    _kubectl("-n", "default", "create", "serviceaccount", "backend-engineer-1",
             check=False)  # idempotent across reruns
    try:
        token = mint_token("default", "backend-engineer-1")
        assert gateway_status(env, token) == 401
        wait_for_authn_deny(
            "authn_namespace_mismatch",
            "system:serviceaccount:default:backend-engineer-1",
        )
    finally:
        _kubectl("-n", "default", "delete", "serviceaccount", "backend-engineer-1",
                 check=False)


def test_wrong_audience_is_rejected(stack: None, env) -> None:
    """A token for another audience — even from the right ServiceAccount —
    fails the TokenReview and is audited as a rejected token."""
    token = mint_token(PLATFORM_NS, "orchestrator-worker", audience="not-the-gateway")
    assert gateway_status(env, token) == 401
    wait_for_authn_deny("authn_token_rejected", "unknown")
