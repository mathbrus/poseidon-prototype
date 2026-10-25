"""Workload identity on kind.

Positive half: the in-cluster callers hold no static token (their env is
checked structurally) and still authenticate — a token minted for the
orchestrator's ServiceAccount is accepted, and the standing agent's own
mounted projected token round-trips against the gateway from inside its
pod. The happy-path suite passing on this cluster is the full-system proof;
these tests isolate the credential path.

Negative half: a token from the wrong service account, the wrong
namespace, or with the wrong audience is rejected *and audited* — the
audit store must show an `authn_*` deny for it.
"""

import time
from typing import Any

import httpx

from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    authn_denies,
    gateway_url,
    kubectl,
    projected_token,
    run_kubectl,
)


def gateway_status(env: dict[str, str], token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(f"{gateway_url(env)}/mcp", headers=headers, timeout=10).status_code


def wait_for_authn_deny(rule: str, principal: str) -> list[dict[str, Any]]:
    """The gateway's own audit of a refused token, once it lands."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        events = authn_denies(rule, principal, "gateway")
        if events:
            return events
        time.sleep(1)
    raise AssertionError(f"no audited {rule} deny for {principal}")


# -- positive: projected tokens are the working credential ------------------


def test_no_static_gateway_credential_in_cluster_pods(stack: None) -> None:
    """The workload-identity contract ('all callers authenticate with projected
    tokens') held structurally: neither the worker nor the agent pod
    carries a bearer-token env var, so every green run on this cluster rode
    the projected tokens."""
    worker_env = kubectl(
        "-n",
        PLATFORM_NS,
        "get",
        "deployment/orchestrator-worker",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].env[*].name}",
    ).split()
    assert "ORCHESTRATOR_BEARER_TOKEN" not in worker_env
    assert "GATEWAY_TOKEN_FILE" in worker_env

    # The blueprint's one standing agent: the PM.
    agent_env = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "deployment/pm",
        "-o",
        "jsonpath={.spec.template.spec.containers[0].env[*].name}",
    ).split()
    assert "AGENT_BEARER_TOKEN" not in agent_env
    assert "GATEWAY_TOKEN_FILE" in agent_env


def test_orchestrator_projected_token_is_accepted(stack: None, env) -> None:
    token = projected_token(PLATFORM_NS, "orchestrator-worker")
    assert gateway_status(env, None) == 401  # control: no credential
    assert gateway_status(env, token) != 401


def test_agent_pod_token_round_trips_from_inside(stack: None) -> None:
    """The agent's actual mounted token, used from its own pod: proves the
    projection, the reconciler-created ServiceAccount, the registry mapping
    and the TokenReview path end to end."""
    pod = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "pod",
        "-l",
        "app=pm",
        "--field-selector=status.phase=Running",
        "-o",
        "jsonpath={.items[0].metadata.name}",
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
    code = kubectl("-n", COMPANY_NS, "exec", pod, "--", "python", "-c", probe)
    assert code != "401", "the agent's own projected token was rejected"


# -- negative: wrong principal, namespace, audience — rejected and audited --


def test_wrong_service_account_is_rejected_and_audited(stack: None, env) -> None:
    token = projected_token("default", "default")
    assert gateway_status(env, token) == 401
    events = wait_for_authn_deny(
        "authn_unknown_service_account", "system:serviceaccount:default:default"
    )
    assert all(e["decision"] == "deny" for e in events)


def test_wrong_namespace_same_name_is_rejected_and_audited(stack: None, env) -> None:
    """A ServiceAccount named like a real instance but living elsewhere
    verifies fine at the API server and must still map to nothing."""
    run_kubectl("-n", "default", "create", "serviceaccount", "pm-1")  # idempotent
    try:
        token = projected_token("default", "pm-1")
        assert gateway_status(env, token) == 401
        wait_for_authn_deny(
            "authn_namespace_mismatch",
            "system:serviceaccount:default:pm-1",
        )
    finally:
        run_kubectl("-n", "default", "delete", "serviceaccount", "pm-1")


def test_wrong_audience_is_rejected(stack: None, env) -> None:
    """A token for another audience — even from the right ServiceAccount —
    fails the TokenReview and is audited as a rejected token."""
    token = projected_token(
        PLATFORM_NS, "orchestrator-worker", audience="not-the-gateway"
    )
    assert gateway_status(env, token) == 401
    wait_for_authn_deny("authn_token_rejected", "unknown")
