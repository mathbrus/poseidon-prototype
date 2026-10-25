"""Ephemeral identity on the live cluster: a bound ephemeral
identity — a per-run ServiceAccount whose projected token round-trips the
real TokenReview path — calls the gateway successfully; a foreign `run_id`
claim from the same *live* identity is policy-denied, and so is every tool
outside its compiled allow-list; the same call after its
`run_participants` row is terminated is denied while the token itself is
still valid — revocation is the registry write, not the clock.

The real fulfill machinery runs here: the actual `CrewActivities` bind and
revoke against the cluster's registry (port-forwarded), then the real
gateway resolves the SA against the row. Lifecycle events and the authn
deny all land in the one audit story.
"""

import asyncio
import time

import httpx

from common.audit import LIFECYCLE_BIND, LIFECYCLE_REVOKE
from common.ids import new_run_id

from .cluster import (
    COMPANY,
    COMPANY_NS,
    audit_story,
    crew_activities,
    gateway_url,
    kubectl,
    mcp_call,
    projected_token,
    registry,
    run_kubectl,
    worker_settings,
)


def test_bound_identity_lives_until_the_row_dies(stack: None, env: dict, tmp_path):
    from registry import Runs

    run_id = new_run_id()
    mcp = f"{gateway_url(env)}/mcp"

    with registry() as client:
        crew = crew_activities(client, worker_settings(env, tmp_path))
        Runs(client).start(run_id, "identity contract", "implement", None, COMPANY)
        sa_name = None
        try:
            bound = asyncio.run(
                crew.bind_participant(
                    {
                        "run_id": run_id,
                        "company": COMPANY,
                        "crew_template": "feature-delivery",
                        "role": "implementer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            # The provisioner would create the SA alongside the pod; this
            # test exercises identity alone, so it creates the SA the same
            # way and mints the same kind of projected token the kubelet
            # would mount. Revocation must beat the token's 10m duration.
            sa_name = bound["service_account"]
            kubectl("-n", COMPANY_NS, "create", "serviceaccount", sa_name)
            token = projected_token(COMPANY_NS, sa_name)

            # The bound identity authenticates at the real gateway ...
            accepted = httpx.get(
                mcp, headers={"Authorization": f"Bearer {token}"}, timeout=10
            )
            assert accepted.status_code != 401, "minted identity rejected"

            # ... and its compiled crew rules answer: the implementer's bound
            # sandbox call passes policy (no sandbox is registered, so the
            # backend errors — but it is not a deny).
            result = mcp_call(
                env, token, "sandbox.list_dir", {"run_id": run_id, "path": "."}
            )
            assert result.isError
            text = result.content[0].text
            assert "denied by policy" not in text, text
            assert "no sandbox registered" in text

            # A foreign run_id claim from a *live* ephemeral agent: authn
            # passes — the row is alive — but the enrichment binds the
            # identity to exactly one run, so policy fails it closed.
            foreign = mcp_call(
                env, token, "sandbox.list_dir", {"run_id": "run-not-mine", "path": "."}
            )
            assert foreign.isError
            assert "default_deny" in foreign.content[0].text

            # Outside its compiled allow-list, the same live identity is
            # denied by rule: PM-only and platform-only tools, and closing a
            # ticket or a pull request — the implementer never grades its
            # own homework.
            repo = env["TARGET_REPO"]
            for tool, args in (
                (
                    "issues.create",
                    {
                        "run_id": run_id,
                        "repo": repo,
                        "title": "x",
                        "body": "y",
                        "rationale": "red-team: probing the allowlist",
                    },
                ),
                (
                    "git.merge_pr",
                    {"run_id": run_id, "repo": repo, "number": 1, "sha": "0" * 40},
                ),
                (
                    "issues.close",
                    {
                        "run_id": run_id,
                        "repo": repo,
                        "number": 1,
                        "reason": "completed",
                        "rationale": "red-team: closing my own ticket",
                    },
                ),
                (
                    "git.close_pr",
                    {
                        "run_id": run_id,
                        "repo": repo,
                        "number": 1,
                        "delete_branch": False,
                        "rationale": "red-team: closing a rival pull request",
                    },
                ),
            ):
                denied = mcp_call(env, token, tool, args)
                assert denied.isError, f"{tool} was not refused"
                assert "default_deny" in denied.content[0].text, (
                    f"{tool}: {denied.content[0].text}"
                )

            # Revocation = row termination, long before the token expires.
            swept = asyncio.run(crew.revoke_participants(run_id))
            assert bound["agent_id"] in swept

            revoked = httpx.get(
                mcp, headers={"Authorization": f"Bearer {token}"}, timeout=10
            )
            assert revoked.status_code == 401, (
                "a revoked identity's still-valid token was accepted"
            )

            # The whole episode is one story: bind, the allowed call, the
            # revoke, and the post-revocation authn deny.
            story = audit_story(run_id)
            lifecycle = [
                (e["tool"], e["rule"])
                for e in story
                if e["source"] == "process-lifecycle"
            ]
            assert (LIFECYCLE_BIND, "participant_bound") in lifecycle
            assert (LIFECYCLE_REVOKE, "participant_revoked") in lifecycle
            gateway_events = [
                e
                for e in story
                if e["source"] == "gateway" and e["agent_id"] == bound["agent_id"]
            ]
            assert gateway_events, "the bound identity's call left no event"
            assert all(e["lifecycle"] == "ephemeral" for e in gateway_events)
            assert all(e["parent_agent_id"] == "pm-1" for e in gateway_events)
            denied_tools = {e["tool"] for e in gateway_events if e["decision"] == "deny"}
            assert {
                "issues.create",
                "git.merge_pr",
                "issues.close",
                "git.close_pr",
            } <= denied_tools, denied_tools

            # The authn deny is recorded too (run_id '' — the credential no
            # longer maps to a run). The principal is the SA username, since
            # the row that named the agent_id is gone.
            deadline = time.monotonic() + 30
            principal = f"system:serviceaccount:{COMPANY_NS}:{sa_name}"
            while True:
                denies = client.query(
                    "SELECT rule FROM registry.audit_events"
                    " WHERE agent_id = %s"
                    "   AND rule = 'authn_unknown_service_account'"
                    "   AND ts > now() - interval '5 minutes'",
                    (principal,),
                )
                if denies:
                    break
                assert time.monotonic() < deadline, "no audited authn deny"
                time.sleep(1)
        finally:
            if sa_name:
                run_kubectl("-n", COMPANY_NS, "delete", "serviceaccount", sa_name)
            client.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            client.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
