"""Review on the live cluster: the full chain — the strategy scoped into a
ticket, a person's `poseidon start implement <number>` implementing it by hand (the
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
which is what makes `poseidon start implement <number>` a repro rather than a race.
"""

import json
import re
import secrets
import subprocess
import time

import pytest

from common.audit import (
    LIFECYCLE_BIND,
    LIFECYCLE_FULFILL,
    LIFECYCLE_PROVISION_POD,
    LIFECYCLE_RECORD_REQUEST,
    LIFECYCLE_REVOKE,
    gate_event,
)
from common.policy import GATE_RESOLVE, MERGE_RESOLVE
from common.tasks import issue_run_task, review_run_task
from common.workflows import MERGE_GATE
from mock_brain.brain import NEXT_SLICE_MARKER

from .blueprints import E2E_GATE_MINUTES, HELLO_SAAS, may_start, restore_pristine
from .cluster import HUMAN, audit_story, await_run, console_env, run_by_task
from .conftest import (
    REPO_ROOT,
    apply_profile,
    assert_merge_closed_tickets,
    await_gate,
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


def _poseidon(env: dict, *args: str, timeout: int = 1800) -> subprocess.CompletedProcess:
    """The real CLI, as the human `owner` against the cluster's console."""
    proc = subprocess.run(
        ["uv", "run", "poseidon", *args],
        cwd=REPO_ROOT,
        env=console_env(env),
        capture_output=True,
        text=True,
        timeout=timeout,
    )
    assert proc.returncode == 0, f"poseidon {args[0]}: {proc.stderr}\n{proc.stdout}"
    return proc


def _poseidon_run(env: dict, issue_number: int) -> dict:
    """A run by hand: `poseidon start implement <number>` through the real
    CLI, blocking until the run record prints."""
    return json.loads(
        _poseidon(env, "start", "implement", str(issue_number), "--json").stdout
    )


def _watch_review_gate(env: dict, run_id: str, pr_number: int) -> dict:
    """Watch the loop's review park on the merge gate, wait for the
    escalation to land in the audit store, then approve through the real
    CLI — the exact HITL sequence `poseidon verdict <run> merge approve`
    answers —
    and read the run to its end."""
    await_gate(run_id, "parked-merge_gate")

    # Nobody answers: the timer escalates at half the declared deadline and
    # the escalation is a real process-gate row — observable, not a log line.
    deadline = time.monotonic() + E2E_GATE_MINUTES * 60
    while time.monotonic() < deadline:
        if any(
            e["source"] == "process-gate" and e["rule"] == "gate_escalated"
            for e in audit_story(run_id)
        ):
            break
        time.sleep(2)
    else:
        raise AssertionError("no process-gate escalation event before the deadline")

    # The human answers through the console: the verdict is recorded under
    # their name, then delivered — and the release reads the record.
    _poseidon(
        env,
        "verdict",
        run_id,
        "merge",
        "approve",
        "--reason",
        "reviewed: findings are clean",
        timeout=120,
    )
    return await_run(review_run_task(pr_number), terminal=True, workflow="review")


@pytest.fixture(scope="module")
def review_chain(stack: None, baseline_env: dict) -> dict:
    """The whole arc: the strategy becomes a ticket (the loop's scope run),
    the ticket becomes a PR (the operator's `poseidon start implement`), the PR becomes
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
        chain["implement"] = _poseidon_run(env, chain["issue_number"])
        pr_url = chain["implement"]["pr_url"]
        match = re.search(r"/pull/(\d+)$", pr_url or "")
        assert match, f"implement produced no usable PR: {chain['implement']}"
        chain["pr_number"] = int(match.group(1))
        chain["pr_url"] = pr_url

        # 3. Review, the loop's: findings -> parked gate -> escalation ->
        # approval -> merge.
        wake_until_run_started(review_run_task(chain["pr_number"]), "review")
        review_run = run_by_task(review_run_task(chain["pr_number"]), workflow="review")
        chain["review_run_id"] = review_run["run_id"]
        chain["review"] = _watch_review_gate(
            env, chain["review_run_id"], chain["pr_number"]
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


def test_the_merge_closed_the_ticket_it_fulfilled(review_chain: dict) -> None:
    """The company's own ticket does not outlive its merged work: the
    platform closed it right after the merge, the merge named as the
    reason, and no role touched it."""
    assert_merge_closed_tickets(
        review_chain["env"],
        audit_story(review_chain["review_run_id"]),
        review_chain["pr_number"],
        [review_chain["issue_number"]],
    )


def test_the_run_by_hand_was_the_only_implement(review_chain: dict) -> None:
    """The loop yielded the ticket: the one implement run on it is the
    person's (not an auto- id), and no second run raced it."""
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
    recorded = [e for e in audit_story(run_id) if e["tool"] == LIFECYCLE_RECORD_REQUEST]
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
        assert event["tool"] == gate_event(MERGE_GATE)
        assert event["workflow"] == f"review-{run_id}"
    escalated, approved = gate_events
    assert escalated["decision"] == "deny"
    assert approved["decision"] == "allow"


def test_the_release_read_the_recorded_verdict(review_chain: dict) -> None:
    """The approval is not the signal: the console recorded the verdict
    under the human's name, the workflow read it back, and the compiled
    `merge.resolve` released the merge on its human path — three rows in
    the one story, in that order, naming the same person."""
    run_id = review_chain["review_run_id"]
    story = audit_story(run_id)
    verdicts = [
        (i, e)
        for i, e in enumerate(story)
        if e["source"] == "console" and e["tool"] == GATE_RESOLVE
    ]
    assert len(verdicts) == 1, verdicts
    index, verdict = verdicts[0]
    assert (verdict["agent_id"], verdict["lifecycle"], verdict["decision"]) == (
        HUMAN,
        "human",
        "allow",
    )
    assert verdict["rule"] == "gate_resolve_merge_approve"
    assert verdict["workflow"] == f"review-{run_id}"
    resolves = [(i, e) for i, e in enumerate(story) if e["tool"] == MERGE_RESOLVE]
    assert resolves, "the merge was never decided"
    release_index, release = resolves[-1]
    assert (release["decision"], release["rule"]) == ("allow", "merge_resolve_human")
    assert release["source"] == "process-lifecycle"
    assert index < release_index, "the verdict was not recorded before the release"


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
        LIFECYCLE_RECORD_REQUEST,
        LIFECYCLE_BIND,
        LIFECYCLE_PROVISION_POD,
        LIFECYCLE_FULFILL,
        LIFECYCLE_REVOKE,
    ):
        assert lifecycle_step in tools, f"{lifecycle_step} missing from the story"
    assert all(e["run_id"] == run_id for e in story)
