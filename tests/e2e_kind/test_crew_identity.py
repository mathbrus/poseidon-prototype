"""Steps 16a + 16b done-when, on the live cluster: a minted ephemeral
identity calls the gateway successfully; a foreign `run_id` claim from the
same *live* identity is policy-denied; the same call after its participants
row is terminated is denied *before* the token's TTL expires — revocation
is the registry write, not the clock; and OPA demonstrably receives the
binding as input, never as pushed data.

The real fulfill machinery runs here: the actual `CrewActivities` mint and
revoke against the cluster's registry (port-forwarded) with the cluster's
platform signing key, then the real gateway verifies. Lifecycle events and
the authn deny all land in the one audit story.
"""

import asyncio
import contextlib
import socket
import subprocess
import time

import httpx
import pytest

from common.ids import new_run_id

from .conftest import PLATFORM_NS, audit_story
from .test_governance import _call

PG_LOCAL_PORT = 15434


def _cluster_secret(name: str, key: str) -> str:
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "get",
            "secret",
            name,
            "-o",
            f'go-template={{{{index .data "{key}" | base64decode}}}}',
        ],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    )
    assert proc.stdout
    return proc.stdout


@contextlib.contextmanager
def _registry():
    """The cluster's registry over a port-forward — the same accessors the
    in-cluster worker uses, pointed at the same database."""
    proc = subprocess.Popen(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "port-forward",
            "svc/postgres",
            f"{PG_LOCAL_PORT}:5432",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail("port-forward to postgres exited early")
            try:
                with socket.create_connection(("127.0.0.1", PG_LOCAL_PORT), timeout=1):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("port-forward to postgres never came up")
                time.sleep(0.5)
        from registry import open_registry

        password = _cluster_secret("postgres-credentials", "POSTGRES_PASSWORD")
        registry = open_registry(
            f"postgresql://poseidon:{password}@127.0.0.1:{PG_LOCAL_PORT}/poseidon",
            migrate=False,
        )
        try:
            yield registry
        finally:
            registry.close()
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def test_minted_identity_lives_until_the_row_dies(stack: None, env: dict, tmp_path):
    from orchestrator.activities.crew import CrewActivities
    from orchestrator.config import WorkerSettings
    from registry import AuditStore, RunParticipants, Runs

    run_id = new_run_id()
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    token_file = tmp_path / "unused-token"
    token_file.write_text("unused")

    with _registry() as registry:
        settings = WorkerSettings(
            gateway_token_file=str(token_file),
            mcp_gateway_url=gateway_url,
            target_repo=env.get("TARGET_REPO", "github.com/example/repo"),
            platform_jwt_key=_cluster_secret("platform-jwt-key", "PLATFORM_JWT_KEY"),
            ephemeral_ttl_s=900,  # revocation must beat this by a mile
        )
        crew = CrewActivities(settings, RunParticipants(registry), AuditStore(registry))
        runs = Runs(registry)
        runs.start(run_id, "16a identity contract", "implement", None, "hello-saas")
        try:
            minted = asyncio.run(
                crew.mint_participant(
                    {
                        "run_id": run_id,
                        "company": "hello-saas",
                        "crew_template": "feature-delivery",
                        "member": "executor",
                        "role_template": "backend-engineer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            token = minted["token"]

            # The minted identity authenticates at the real gateway ...
            accepted = httpx.get(
                f"{gateway_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert accepted.status_code != 401, "minted identity rejected"

            # ... and its compiled crew rules answer: the executor's bound
            # sandbox call passes policy (no sandbox is registered, so the
            # backend errors — but it is not a deny).
            result = _call(
                env, token, "sandbox.list_dir", {"run_id": run_id, "path": "."}
            )
            assert result.isError
            text = result.content[0].text
            assert "denied by policy" not in text, text
            assert "no sandbox registered" in text

            # A foreign run_id claim from a *live* participant (16b): authn
            # passes — the row is alive — but the enrichment binds the
            # identity to exactly one run, so policy fails it closed.
            foreign = _call(
                env, token, "sandbox.list_dir", {"run_id": "run-not-mine", "path": "."}
            )
            assert foreign.isError
            assert "default_deny" in foreign.content[0].text

            # Revocation = row termination, long before TTL.
            swept = asyncio.run(crew.revoke_participants(run_id))
            assert minted["agent_id"] in swept

            revoked = httpx.get(
                f"{gateway_url}/mcp",
                headers={"Authorization": f"Bearer {token}"},
                timeout=10,
            )
            assert revoked.status_code == 401, (
                "a revoked identity's still-valid token was accepted"
            )

            # The whole episode is one story: mint, the allowed call, the
            # revoke, and the post-revocation authn deny.
            story = audit_story(run_id)
            lifecycle = [
                (e["tool"], e["rule"])
                for e in story
                if e["source"] == "process-lifecycle"
            ]
            assert ("lifecycle.mint", "participant_minted") in lifecycle
            assert ("lifecycle.revoke", "participant_revoked") in lifecycle
            gateway_events = [
                e
                for e in story
                if e["source"] == "gateway" and e["agent_id"] == minted["agent_id"]
            ]
            assert gateway_events, "the minted identity's call left no event"
            assert all(e["lifecycle"] == "ephemeral" for e in gateway_events)
            assert all(e["parent_agent_id"] == "pm-1" for e in gateway_events)

            # The authn deny is recorded too (run_id '' — the credential no
            # longer maps to a run).
            deadline = time.monotonic() + 30
            while True:
                denies = registry.query(
                    "SELECT rule FROM registry.audit_events"
                    " WHERE agent_id = %s AND rule = 'authn_no_live_participant'"
                    "   AND ts > now() - interval '5 minutes'",
                    (minted["agent_id"],),
                )
                if denies:
                    break
                assert time.monotonic() < deadline, "no audited authn deny"
                time.sleep(1)
        finally:
            registry.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            registry.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


def test_binding_rides_the_input_never_pushed_data(stack: None) -> None:
    """Against the running OPA: there is no pushed run data anywhere — the
    policy reads the enriched request *input* rather than a pushed
    data.runs.assignments document — and the same live bundle flips the
    executor's decision on the input flag alone."""
    from .test_operator_lifecycle import port_forward

    with port_forward("opa", 18183, 8181) as base:
        # Nothing was ever pushed: the old assignments document does not
        # exist, and nothing else lives outside the served bundles.
        pushed = httpx.get(f"{base}/v1/data/runs", timeout=10).json()
        assert "result" not in pushed or not pushed["result"], (
            f"OPA carries pushed run data: {pushed}"
        )

        executor = {
            "company": "hello-saas",
            "agent_id": "feature-delivery-executor-x",
            "lifecycle": "ephemeral",
            "parent_agent_id": "pm-1",
            "role": "backend-engineer",
            "crew_template": "feature-delivery",
            "crew_role": "executor",
            "run_id": "run-16b",
            "workflow": "implement",
            "tool": "sandbox.exec",
            "args": {},
        }
        bound = httpx.post(
            f"{base}/v1/data/poseidon/authz/hello_saas/decision",
            json={"input": {**executor, "run_binding_verified": True}},
            timeout=10,
        ).json()["result"]
        unbound = httpx.post(
            f"{base}/v1/data/poseidon/authz/hello_saas/decision",
            json={"input": {**executor, "run_binding_verified": False}},
            timeout=10,
        ).json()["result"]
        assert bound["allow"] is True
        assert unbound == {"allow": False, "rule": "default_deny"}
