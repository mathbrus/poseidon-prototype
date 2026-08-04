"""Step 16a done-when, on the live cluster: a minted ephemeral identity
calls the gateway successfully, and the same call after its participants
row is terminated is denied *before* the token's TTL expires — revocation
is the registry write, not the clock.

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

from .conftest import COMPANY_NS, PLATFORM_NS, audit_story
from .test_governance import _call

PG_LOCAL_PORT = 15434


def _cluster_secret(name: str, key: str) -> str:
    proc = subprocess.run(
        ["kubectl", "-n", PLATFORM_NS, "get", "secret", name,
         "-o", f"go-template={{{{index .data \"{key}\" | base64decode}}}}"],
        capture_output=True, text=True, timeout=60, check=True,
    )
    assert proc.stdout
    return proc.stdout


@contextlib.contextmanager
def _registry():
    """The cluster's registry over a port-forward — the same accessors the
    in-cluster worker uses, pointed at the same database."""
    proc = subprocess.Popen(
        ["kubectl", "-n", PLATFORM_NS, "port-forward", "svc/postgres",
         f"{PG_LOCAL_PORT}:5432"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
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
            agent_url="http://unused.test:8000",
            target_repo=env.get("TARGET_REPO", "github.com/example/repo"),
            platform_jwt_key=_cluster_secret("platform-jwt-key", "PLATFORM_JWT_KEY"),
            ephemeral_ttl_s=900,  # revocation must beat this by a mile
        )
        crew = CrewActivities(
            settings, RunParticipants(registry), AuditStore(registry)
        )
        runs = Runs(registry)
        runs.start(run_id, "16a identity contract", "implement", None, "hello-saas")
        try:
            minted = asyncio.run(crew.mint_participant({
                "run_id": run_id,
                "company": "hello-saas",
                "crew_template": "feature-delivery",
                "member": "executor",
                "role_template": "backend-engineer",
                "parent_agent_id": "pm-1",
            }))
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
            result = _call(env, token, "sandbox.list_dir",
                           {"run_id": run_id, "path": "."})
            assert result.isError
            text = result.content[0].text
            assert "denied by policy" not in text, text
            assert "no sandbox registered" in text

            # Revocation = row termination (§2b), long before TTL.
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
            lifecycle = [(e["tool"], e["rule"]) for e in story
                         if e["source"] == "process-lifecycle"]
            assert ("lifecycle.mint", "participant_minted") in lifecycle
            assert ("lifecycle.revoke", "participant_revoked") in lifecycle
            gateway_events = [e for e in story if e["source"] == "gateway"
                              and e["agent_id"] == minted["agent_id"]]
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
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,))
            registry.execute(
                "DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
