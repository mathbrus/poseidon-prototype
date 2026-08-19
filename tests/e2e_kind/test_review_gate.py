"""Review on the live cluster: the full chain — intake tickets,
implement PR (started from the intake ticket's issue number, the
`poseidon run <number>` handoff: no human retypes the spec), reviewer
findings posted, merge gate parked, timer escalation observable, API
approval, gateway merge — every step correlated by run_id in one audit
story (MVP exit criterion 3).

The chain runs its own three runs (never the shared session fixtures: this
module *merges* its PR into the disposable repo's main, and nothing else in
the suite may inherit that side effect). The merge gate leg is timed by the
fast profile: the gate parks, escalates at merge_gate_s/2 with a real
`process-gate` audit event, and only then does the test approve — so the
escalation path is proven on every run, not just when someone forgets to
answer.
"""

import asyncio
import contextlib
import re
import time

import httpx
import pytest

from common.ids import new_run_id
from orchestrator.workflows.implement import Timeouts, issue_placeholder
from orchestrator.workflows.intake import IntakeInput, intake_workflow_id
from orchestrator.workflows.review import (
    ReviewInput,
    review_workflow_id,
)

from .conftest import (
    TASK_QUEUE,
    _psql_json,
    audit_story,
    close_pr,
    deployed_timeouts,
    run_implement_from_issue,
    temporal_port_forward,
)
from .test_crew_identity import _registry


def _reviewer_id(run_id: str) -> str:
    from orchestrator.identity import ephemeral_agent_id

    return ephemeral_agent_id("feature-review", "reviewer", run_id)


def _github(env: dict, method: str, path: str, **kwargs) -> httpx.Response:
    return httpx.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {env['GIT_TOKEN']}",
        },
        timeout=30,
        **kwargs,
    )


def _repo_slug(env: dict) -> str:
    return "/".join(env["TARGET_REPO"].split("/")[-2:])


async def _execute_review_with_gate(address: str, run_id: str, pr_number: int) -> dict:
    """Start review detached, watch it park on the merge gate, wait for the
    escalation to land in the audit store, then approve — the exact HITL
    sequence `poseidon review --detach` + `poseidon signal ... merge` runs."""
    from temporalio.client import Client

    client = await Client.connect(address)
    handle = await client.start_workflow(
        "review",
        ReviewInput(run_id=run_id, pr_number=pr_number, timeouts=deployed_timeouts()),
        id=review_workflow_id(run_id),
        task_queue=TASK_QUEUE,
    )

    # The park is an API-visible pending state, reached only after the
    # reviewer posted findings.
    deadline = time.monotonic() + 600
    parked = False
    while time.monotonic() < deadline:
        state = await handle.query("state")
        if state == "paused-merge_gate":
            parked = True
            break
        await asyncio.sleep(2)
    assert parked, "the review workflow never parked on the merge gate"

    # Nobody answers: the timer escalates at merge_gate_s/2 and the
    # escalation is a real process-gate row — observable, not a log line.
    deadline = time.monotonic() + (deployed_timeouts() or Timeouts()).merge_gate_s
    escalated = False
    while time.monotonic() < deadline:
        if any(
            e["source"] == "process-gate" and e["rule"] == "gate_escalated"
            for e in audit_story(run_id)
        ):
            escalated = True
            break
        await asyncio.sleep(2)
    assert escalated, "no process-gate escalation event before the deadline"

    # The human answers through the API — approval releases the merge.
    await handle.signal("merge", args=[True, "reviewed: findings are clean"])
    record = await asyncio.wait_for(handle.result(), timeout=300)
    return record


@pytest.fixture(scope="module")
def review_chain(stack: None, env: dict) -> dict:
    """The whole arc: a goal becomes tickets (intake), a task becomes
    a PR (implement), the PR becomes reviewer findings, a parked gate, an
    escalation, an approval, and a gateway merge (review)."""
    chain: dict = {"env": env}

    # 1. Intake: tickets + recorded intent under the PM's authority. The
    # goal carries the delivery-branch line, so the ticket the PM files
    # carries it too — the issue is the whole spec the implement run
    # consumes, and the nonce branch keeps reruns against the disposable
    # repo from colliding.
    intake_run = new_run_id()
    nonce = intake_run[-6:].lower()
    goal = (
        f"Ship the slugify utility to production (e2e review-chain goal {nonce})\n\n"
        f"Deliver your work on the branch `feature/slugify-{nonce}`.\n"
    )

    async def start_intake() -> dict:
        from temporalio.client import Client

        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = await client.start_workflow(
                "intake",
                IntakeInput(
                    run_id=intake_run, goal_text=goal, timeouts=deployed_timeouts()
                ),
                id=intake_workflow_id(intake_run),
                task_queue=TASK_QUEUE,
            )
            return await asyncio.wait_for(handle.result(), timeout=600)

    chain["intake_run_id"] = intake_run
    chain["intake"] = asyncio.run(start_intake())

    # 2. Implement, from the ticket intake just filed — the company
    # consumes its own artifact; nobody writes a second spec. The chain
    # gets its own PR (this module merges it — the shared session
    # fixtures' PRs must stay untouched).
    ticket_url = (chain["intake"].get("ticket_urls") or [""])[0]
    issue_match = re.search(r"/issues/(\d+)$", ticket_url)
    assert issue_match, f"intake filed no usable ticket: {chain['intake']}"
    chain["ticket_url"] = ticket_url
    chain["issue_number"] = int(issue_match.group(1))
    chain["implement"] = run_implement_from_issue(chain["issue_number"], env)
    pr_url = chain["implement"]["pr_url"]
    match = re.search(r"/pull/(\d+)$", pr_url or "")
    assert match, f"implement produced no usable PR: {chain['implement']}"
    chain["pr_number"] = int(match.group(1))
    chain["pr_url"] = pr_url

    # 3. Review: findings -> parked gate -> escalation -> approval -> merge.
    review_run = new_run_id()
    chain["review_run_id"] = review_run
    with temporal_port_forward() as address:
        chain["review"] = asyncio.run(
            _execute_review_with_gate(address, review_run, chain["pr_number"])
        )

    yield chain

    # The intake leg's leftovers, exactly as test_intake cleans its own.
    # The merged PR's branch goes too; the merge itself is what the
    # assertions read, and it survives the branch.
    close_pr(env, chain["pr_number"])
    slug = _repo_slug(env)
    for url in chain["intake"].get("ticket_urls") or []:
        number = re.search(r"/issues/(\d+)$", url)
        if number:
            with contextlib.suppress(Exception):
                _github(
                    env,
                    "PATCH",
                    f"/repos/{slug}/issues/{number.group(1)}",
                    json={"state": "closed"},
                )
    with _registry() as registry:
        registry.execute(
            "DELETE FROM registry.crew_requests WHERE run_id = %s", (intake_run,)
        )
        registry.execute("DELETE FROM registry.runs WHERE run_id = %s", (intake_run,))


def test_the_full_chain_lands_a_human_gated_merge(review_chain: dict) -> None:
    """MVP exit criterion 3, end to end: tickets, PR, findings, approval,
    merge — and the PR really is merged on GitHub."""
    assert review_chain["intake"]["status"] == "done"
    assert review_chain["intake"]["ticket_urls"], "intake filed no tickets"
    assert review_chain["implement"]["status"] == "done"
    # The handoff was the ticket itself: the implement run consumed the
    # issue intake filed (linked on its run row), and its task is the
    # issue reference — no second, human-written spec anywhere.
    assert review_chain["implement"]["ticket_urls"] == [review_chain["ticket_url"]]
    assert review_chain["implement"]["task"] == issue_placeholder(
        review_chain["issue_number"]
    )

    record = review_chain["review"]
    assert record["status"] == "done", f"review failed: {record}"
    assert record["workflow"] == "review"
    assert record["pr_url"] == review_chain["pr_url"]

    env = review_chain["env"]
    pr = _github(
        env, "GET", f"/repos/{_repo_slug(env)}/pulls/{review_chain['pr_number']}"
    )
    assert pr.status_code == 200
    assert pr.json()["merged"] is True, "the gateway merge never landed"


def test_findings_are_posted_by_the_ephemeral_reviewer(review_chain: dict) -> None:
    """The reviewer is an ephemeral feature-review crew member under
    the PM's authority — it reads the diff, executes the suite in its own
    sandbox (provisioned from the PR ref), and comments on the PR."""
    run_id = review_chain["review_run_id"]
    reviewer = _reviewer_id(run_id)
    story = audit_story(run_id)
    # Parentage is the gateway pipeline's enrichment (participants row →
    # v2 attribution); lifecycle.revoke rows legitimately carry no parent.
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

    # The findings comment is really on the PR.
    env = review_chain["env"]
    comments = _github(
        env,
        "GET",
        f"/repos/{_repo_slug(env)}/issues/{review_chain['pr_number']}/comments",
    )
    assert comments.status_code == 200
    assert any("test suite" in c["body"] for c in comments.json()), (
        "no findings comment on the PR"
    )


def test_the_crew_request_was_recorded_under_conductor_authority(
    review_chain: dict,
) -> None:
    """Process-initiated: the workflow's own crew request is
    the same crew_requests row and the same audited compiled decision an
    agent-issued request produces."""
    run_id = review_chain["review_run_id"]
    rows = _psql_json(
        "SELECT COALESCE(json_agg(c), '[]'::json) FROM ("
        "SELECT * FROM registry.crew_requests WHERE run_id = :'run_id'"
        ") c",
        run_id,
        default="[]",
    )
    assert len(rows) == 1
    assert rows[0]["conductor"] == "pm-1"
    assert rows[0]["crew_template"] == "feature-review"
    assert rows[0]["status"] == "fulfilled"

    recorded = [e for e in audit_story(run_id) if e["tool"] == "lifecycle.record_request"]
    assert len(recorded) == 1
    assert recorded[0]["decision"] == "allow"
    assert recorded[0]["agent_id"] == "pm-1"
    assert recorded[0]["lifecycle"] == "standing"


def test_gate_escalates_then_records_the_approval(review_chain: dict) -> None:
    """Escalation, then the verdict: both transitions
    are process-gate audit events carrying the workflow id, in order."""
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
    """git.merge_pr is a gateway-audited call under
    the platform lifecycle, and it happens after the gate's approval —
    never before, never around the gateway."""
    run_id = review_chain["review_run_id"]
    story = audit_story(run_id)
    merges = [(i, e) for i, e in enumerate(story) if e["tool"] == "git.merge_pr"]
    assert len(merges) == 1
    merge_index, merge = merges[0]
    assert merge["decision"] == "allow"
    assert merge["lifecycle"] == "platform"
    approvals = [
        i
        for i, e in enumerate(story)
        if e["source"] == "process-gate" and e["rule"] == "gate_approved"
    ]
    assert approvals and max(approvals) < merge_index, (
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
