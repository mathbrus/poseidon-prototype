"""Workload identity on kind.

Positive half: the in-cluster callers hold no static token (their env is
checked structurally) and still authenticate — a token minted for the
orchestrator's ServiceAccount is accepted, and the standing agent's own
mounted projected token round-trips against the gateway from inside its
pod. The happy-path suite passing on this cluster is the full-system proof;
these tests isolate the credential path.

Negative half: a token with the wrong audience is rejected *and audited*
— the audit store shows the `authn_*` deny this test caused. The wrong
ServiceAccount and the wrong namespace are the gateway's own unit tests
(`packages/mcp-gateway/tests/test_authn.py`).
"""

import time
from typing import Any

import httpx

from .cluster import (
    COMPANY_NS,
    PLATFORM_NS,
    authn_denies,
    db_now,
    gateway_url,
    kubectl,
    projected_token,
)


def gateway_status(env: dict[str, str], token: str | None) -> int:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.get(f"{gateway_url(env)}/mcp", headers=headers, timeout=10).status_code


def wait_for_authn_deny(rule: str, principal: str, since: str) -> list[dict[str, Any]]:
    """The gateway's own audit of a token refused after `since` (a
    `db_now` value taken before the call), once it lands — never an
    earlier test's deny of the same principal."""
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        events = authn_denies(rule, principal, "gateway", since)
        if events:
            return events
        time.sleep(1)
    raise AssertionError(f"no audited {rule} deny for {principal}")


# -- positive: projected tokens are the working credential ------------------


def _declared_env(namespace: str, deployment: str) -> dict[str, str]:
    """A deployment's container env as declared: each name, and the Secret
    it is read from (empty for a literal value)."""
    rows = kubectl(
        "-n",
        namespace,
        "get",
        f"deployment/{deployment}",
        "-o",
        "jsonpath={range .spec.template.spec.containers[0].env[*]}"
        '{.name}|{.valueFrom.secretKeyRef.name}{"\\n"}{end}',
    ).splitlines()
    return dict(row.split("|", 1) for row in rows if row)


def test_no_static_gateway_credential_in_cluster_pods(stack: None) -> None:
    """The workload-identity contract ('all callers authenticate with projected
    tokens') held structurally: neither the worker nor the agent pod
    carries a token in its env — each reads the projected token file — so
    every green run on this cluster rode the projected tokens. The one
    secret the worker reads is the egress proxy's key it hands each
    sandbox, which no gateway accepts."""
    worker = _declared_env(PLATFORM_NS, "orchestrator-worker")
    assert "GATEWAY_TOKEN_FILE" in worker
    assert not [name for name in worker if name.endswith("_TOKEN")], worker
    assert {name for name, secret in worker.items() if secret} == {"EGRESS_PROXY_KEY"}

    # The blueprint's one standing agent: the PM. Nothing credential-shaped
    # at all, and nothing read from a Secret.
    agent = _declared_env(COMPANY_NS, "pm")
    assert "GATEWAY_TOKEN_FILE" in agent
    shaped = [
        name for name in agent if name.endswith(("_TOKEN", "_KEY")) and name != "GPG_KEY"
    ]
    assert not shaped, agent
    assert not any(agent.values()), agent


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


def test_wrong_audience_is_rejected(stack: None, env) -> None:
    """A token for another audience — even from the right ServiceAccount —
    fails the TokenReview and is audited as a rejected token."""
    token = projected_token(
        PLATFORM_NS, "orchestrator-worker", audience="not-the-gateway"
    )
    since = db_now()
    assert gateway_status(env, token) == 401
    wait_for_authn_deny("authn_token_rejected", "unknown", since)
