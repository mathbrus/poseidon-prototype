"""Review on the live cluster: the full chain — the strategy scoped into a
ticket, an operator's `poseidon run <number>` implementing it by hand (the
repro command: no human retypes the spec, and the loop yields the ticket
to the operator), reviewer findings posted, merge gate parked, timer
escalation observable, API approval, gateway merge — every step correlated
by run_id in one audit story.

The chain runs its own runs (never the shared session fixtures: this
module *merges* its PR into the disposable repo's main, and nothing else in
the suite may inherit that side effect). The merge gate leg is timed by the
fast profile: the gate parks, escalates at half its declared deadline with
a real `process-gate` audit event, and only then does the test approve —
so the escalation path is proven on every run, not just when someone
forgets to answer.

Applies the example with `may_start: [scope, review]`: the company scopes
and reviews on its own, and implementing is the operator's act here —
which is what makes `poseidon run <number>` a repro rather than a race.
"""

import asyncio
import json
import os
import re
import secrets
import subprocess
import time

import pytest
from temporalio.client import Client

from common.tasks import issue_run_task
from mockllm.brain import NEXT_SLICE_MARKER
from orchestrator.workflows.base import workflow_id

from .blueprints import E2E_GATE_MINUTES, HELLO_SAAS, may_start, restore_pristine
from .cluster import audit_story, run_by_task, temporal_port_forward
from .conftest import (
    REPO_ROOT,
    apply_profile,
    close_issue,
    close_pr,
    github_api,
    open_slice_strategy,
    restore_strategy,
    scope_via_heartbeat,
    target_slug,
    wake_until_run_started,
)


def _reviewer_id(run_id: str) -> str:
    from orchestrator.identity import ephemeral_agent_id

    return ephemeral_agent_id("feature-review", "reviewer", run_id)


def _poseidon_run(issue_number: int, address: str) -> dict:
    """The operator's repro: `poseidon run <number>` through the real CLI,
    blocking until the run record prints."""
    proc = subprocess.run(
        ["uv", "run", "poseidon", "run", str(issue_number)],
        cwd=REPO_ROOT,
        env={**os.environ, "TEMPORAL_ADDRESS": address},
        capture_output=True,
        text=True,
        timeout=1800,
    )
    assert proc.returncode == 0, f"poseidon run: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


async def _watch_review_gate(address: str, run_id: str) -> dict:
    """Watch the loop's review park on the merge gate, wait for the
    escalation to land in the audit store, then approve — the exact HITL
    sequence `poseidon signal ... merge` answers."""
    client = await Client.connect(address)
    handle = client.get_workflow_handle(workflow_id("review", run_id))
    deadline = time.monotonic() + 600
    while time.monotonic() < deadline:
        if await handle.query("state") == "paused-merge_gate":
            break
        await asyncio.sleep(2)
    else:
        raise AssertionError("the review workflow never parked on the merge gate")

    # Nobody answers: the timer escalates at half the declared deadline and
    # the escalation is a real process-gate row — observable, not a log line.
    deadline = time.monotonic() + E2E_GATE_MINUTES * 60
    while time.monotonic() < deadline:
        if any(
            e["source"] == "process-gate" and e["rule"] == "gate_escalated"
            for e in audit_story(run_id)
        ):
            break
        await asyncio.sleep(2)
    else:
        raise AssertionError("no process-gate escalation event before the deadline")

    # The human answers through the API — approval releases the merge.
    await handle.signal("merge", args=[True, "reviewed: findings are clean"])
    return await asyncio.wait_for(handle.result(), timeout=300)


@pytest.fixture(scope="module")
def review_chain(stack: None, baseline_env: dict) -> dict:
    """The whole arc: the strategy becomes a ticket (the loop's scope run),
    the ticket becomes a PR (the operator's `poseidon run`), the PR becomes
    reviewer findings, a parked gate, an escalation, an approval, and a
    gateway merge (the loop's review run)."""
    env = baseline_env
    apply_profile(may_start(HELLO_SAAS, "scope", "review"), env)
    chain: dict = {"env": env}
    nonce = secrets.token_hex(3)
    try:
        # 1. Scope: the strategy names the slice, the loop files the ticket.
        chain["scope"] = scope_via_heartbeat(
            env, open_slice_strategy(nonce, NEXT_SLICE_MARKER)
        )
        ticket_url = (chain["scope"].get("ticket_urls") or [""])[0]
        issue_match = re.search(r"/issues/(\d+)$", ticket_url)
        assert issue_match, f"the scope run filed no usable ticket: {chain['scope']}"
        chain["ticket_url"] = ticket_url
        chain["issue_number"] = int(issue_match.group(1))

        # 2. Implement, by hand: the operator's repro on the company's own
        # ticket. The loop may not implement in this profile, so the ticket
        # is the operator's to start — and the company's spec, not a
        # second, human-written one.
        with temporal_port_forward() as address:
            chain["implement"] = _poseidon_run(chain["issue_number"], address)
        pr_url = chain["implement"]["pr_url"]
        match = re.search(r"/pull/(\d+)$", pr_url or "")
        assert match, f"implement produced no usable PR: {chain['implement']}"
        chain["pr_number"] = int(match.group(1))
        chain["pr_url"] = pr_url

        # 3. Review, the loop's: findings -> parked gate -> escalation ->
        # approval -> merge.
        from common.tasks import review_run_task

        wake_until_run_started(review_run_task(chain["pr_number"]), "review")
        review_run = run_by_task(review_run_task(chain["pr_number"]), workflow="review")
        chain["review_run_id"] = review_run["run_id"]
        with temporal_port_forward() as address:
            chain["review"] = asyncio.run(
                _watch_review_gate(address, chain["review_run_id"])
            )
        yield chain
    finally:
        # The merged PR's branch goes too; the merge itself is what the
        # assertions read, and it survives the branch.
        if chain.get("pr_number"):
            close_pr(env, chain["pr_number"])
        close_issue(env, chain.get("issue_number"))
        restore_strategy(env)
        restore_pristine()


def test_the_full_chain_lands_a_human_gated_merge(review_chain: dict) -> None:
    """The full chain, end to end: ticket, PR, findings, approval,
    merge — and the PR really is merged on GitHub."""
    assert review_chain["scope"]["status"] == "done"
    assert review_chain["scope"]["ticket_urls"], "the scope run filed no tickets"
    assert review_chain["implement"]["status"] == "done"
    # The handoff was the ticket itself: the implement run consumed the
    # issue the company filed (linked on its run row), and its task is the
    # issue reference — no second, human-written spec anywhere.
    assert review_chain["implement"]["ticket_urls"] == [review_chain["ticket_url"]]
    assert review_chain["implement"]["task"] == issue_run_task(
        review_chain["issue_number"]
    )

    record = review_chain["review"]
    assert record["status"] == "done", f"review failed: {record}"
    assert record["workflow"] == "review"
    assert record["pr_url"] == review_chain["pr_url"]

    env = review_chain["env"]
    pr = github_api(
        env, "GET", f"/repos/{target_slug(env)}/pulls/{review_chain['pr_number']}"
    )
    assert pr.status_code == 200
    assert pr.json()["merged"] is True, "the gateway merge never landed"


def test_the_operator_run_was_the_only_implement(review_chain: dict) -> None:
    """The loop yielded the ticket: the one implement run on it is the
    operator's (not an auto- id), and no second run raced it."""
    run = run_by_task(issue_run_task(review_chain["issue_number"]), workflow="implement")
    assert run and run["run_id"] == review_chain["implement"]["run_id"]
    assert not run["run_id"].startswith("auto-"), run
    assert review_chain["review_run_id"].startswith("auto-")


def test_findings_are_posted_by_the_ephemeral_reviewer(review_chain: dict) -> None:
    """The reviewer is an ephemeral feature-review ephemeral agent under
    the PM's authority — it reads the diff, executes the suite in its own
    sandbox (provisioned from the PR ref), and comments on the PR."""
    run_id = review_chain["review_run_id"]
    reviewer = _reviewer_id(run_id)
    story = audit_story(run_id)
    reviewer_events = [
        e for e in story if e["agent_id"] == reviewer and e["source"] == "gateway"
    ]
    tools = [e["tool"] for e in reviewer_events]
    assert "git.read" in tools, "the reviewer never read the diff"
    assert "sandbox.run_tests" in tools, "the reviewer never executed the suite"
    assert "git.comment" in tools, "the reviewer never posted findings"
    for event in reviewer_events:
        assert event["lifecycle"] == "ephemeral"
        assert event["parent_agent_id"] == "pm-1"
        assert event["workflow"] == "review"
        assert event["decision"] == "allow"

    env = review_chain["env"]
    comments = github_api(
        env,
        "GET",
        f"/repos/{target_slug(env)}/issues/{review_chain['pr_number']}/comments",
    )
    assert comments.status_code == 200
    assert any("test suite" in c["body"] for c in comments.json()), "no findings comment"


def test_the_crew_was_fulfilled_under_conductor_authority(review_chain: dict) -> None:
    """Fulfilment won the compiled crew.request decision as the conductor:
    one audited `lifecycle.record_request` allow under the standing pm-1
    before the crew was bound."""
    run_id = review_chain["review_run_id"]
    recorded = [e for e in audit_story(run_id) if e["tool"] == "lifecycle.record_request"]
    assert len(recorded) == 1
    assert recorded[0]["decision"] == "allow"
    assert recorded[0]["agent_id"] == "pm-1"
    assert recorded[0]["lifecycle"] == "standing"


def test_gate_escalates_then_records_the_approval(review_chain: dict) -> None:
    """Escalation, then the verdict: both transitions are process-gate
    audit events carrying the workflow id, in order."""
    run_id = review_chain["review_run_id"]
    gate_events = [e for e in audit_story(run_id) if e["source"] == "process-gate"]
    rules = [e["rule"] for e in gate_events]
    assert rules == ["gate_escalated", "gate_approved"]
    for event in gate_events:
        assert event["tool"] == "gate.merge"
        assert event["workflow"] == f"review-{run_id}"
    escalated, approved = gate_events
    assert escalated["decision"] == "deny"
    assert approved["decision"] == "allow"


def test_merge_is_a_gateway_call_strictly_after_the_gate(review_chain: dict) -> None:
    """git.merge_pr is a gateway-audited call under the platform lifecycle,
    and it happens after the gate's approval — never before, never around
    the gateway. The activity's step retry may re-issue the call when the
    git host errors (a fresh PR whose mergeability GitHub has not computed
    yet answers 405); every attempt is audited, and every one of them must
    still follow the approval."""
    run_id = review_chain["review_run_id"]
    story = audit_story(run_id)
    merges = [(i, e) for i, e in enumerate(story) if e["tool"] == "git.merge_pr"]
    assert merges, "no audited merge call in the story"
    assert all(e["decision"] == "allow" for _, e in merges)
    assert all(e["lifecycle"] == "platform" for _, e in merges)
    assert merges[-1][1]["backend_status"] == "ok", merges
    approvals = [
        i
        for i, e in enumerate(story)
        if e["source"] == "process-gate" and e["rule"] == "gate_approved"
    ]
    assert approvals and max(approvals) < min(i for i, _ in merges), (
        "the merge does not follow the gate approval in the story"
    )


def test_one_story_spans_every_review_source(review_chain: dict) -> None:
    """One run_id query tells the whole story: reviewer tool calls
    (gateway), its model calls (llm-gateway), fulfillment and revocation
    (process-lifecycle), and the gate (process-gate)."""
    run_id = review_chain["review_run_id"]
    story = audit_story(run_id)
    sources = {e["source"] for e in story}
    assert {"gateway", "llm-gateway", "process-lifecycle", "process-gate"} <= sources, (
        sources
    )
    tools = [e["tool"] for e in story]
    for lifecycle_step in (
        "lifecycle.record_request",
        "lifecycle.bind",
        "lifecycle.provision_pod",
        "lifecycle.fulfill",
        "lifecycle.revoke",
    ):
        assert lifecycle_step in tools, f"{lifecycle_step} missing from the story"
    assert all(e["run_id"] == run_id for e in story)
