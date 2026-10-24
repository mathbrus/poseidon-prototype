"""Review on the live cluster: the full chain — the strategy scoped into a
ticket, a person's `poseidon start implement <number>` implementing it by hand (the
repro command: no human retypes the spec, and the loop yields the ticket
to the human), reviewer findings posted, merge gate parked, timer
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
and reviews on its own, and implementing is a human's act here —
which is what makes `poseidon start implement <number>` a repro rather than a race.

The merge names the head the review read. A second arc proves it on the
git host: a pull request the world opened is reviewed by a person's start
and parks; someone pushes to its branch; the person approves; GitHub
refuses the pinned merge, the run fails, and the delivery is refused: the
PR closes with the host's refusal on it.
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

from .blueprints import (
    E2E_GATE_MINUTES,
    E2E_PATIENT_GATE_MINUTES,
    HELLO_SAAS,
    apply_blueprint,
    may_start,
    restore_pristine,
    wait_for_convergence,
)
from .cluster import (
    COMPANY_NS,
    HUMAN,
    audit_story,
    await_run,
    console,
    console_env,
    db_now,
    kubectl,
    participants,
    run_by_task,
    run_record,
    runs_by_task,
    sandbox_pods,
)
from .conftest import (
    REPO_ROOT,
    apply_profile,
    assert_merge_closed_tickets,
    await_gate,
    close_issue,
    close_pr,
    file_ticket,
    github_api,
    issue_state,
    open_slice_strategy,
    open_world_pr,
    pr_number,
    put_branch_file,
    restore_strategy,
    scope_via_heartbeat,
    target_slug,
    ticket_body,
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
    the ticket becomes a PR (a human's `poseidon start implement`), the PR becomes
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

        # 2. Implement, by hand: a human's repro on the company's own
        # ticket. The loop may not implement in this profile, so the ticket
        # is a human's to start — and the company's spec, not a
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
        close_issue(env, chain.get("issue_number"))
        if chain.get("pr_number"):
            close_pr(env, chain["pr_number"])
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
    """The park, the escalation, then the verdict: every transition is a
    process-gate audit event carrying the workflow id, in order."""
    run_id = review_chain["review_run_id"]
    gate_events = [e for e in audit_story(run_id) if e["source"] == "process-gate"]
    rules = [e["rule"] for e in gate_events]
    assert rules == ["gate_parked", "gate_escalated", "gate_approved"]
    for event in gate_events:
        assert event["tool"] == gate_event(MERGE_GATE)
        assert event["workflow"] == f"review-{run_id}"
    parked, escalated, approved = gate_events
    assert parked["decision"] == escalated["decision"] == "deny"
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


# -- the merged head is the reviewed head -----------------------------------


@pytest.fixture(scope="module")
def moved_head(stack: None, baseline_env: dict) -> dict:
    """A pull request the world opened — the heartbeat observes only the
    PRs the company delivered, so it starts nothing on this one — reviewed
    by a person's start and parked on the human gate. While it waits, a
    push moves its branch; then the person approves."""
    env = baseline_env
    nonce = secrets.token_hex(3)
    branch = f"feature/moved-{nonce}"
    path = f"docs/notes-{nonce}.md"
    result: dict = {"env": env}
    try:
        number = open_world_pr(
            env, branch, {path: "# Notes\n\nReviewed text.\n"}, f"Add notes ({nonce})"
        )
        result["pr_number"] = number
        run_id = console().start_run("review", number).run_id
        result["run_id"] = run_id
        # The workflow writes the run's row; the console knows the run
        # only from then on.
        await_run(review_run_task(number), terminal=False, workflow="review")
        await_gate(run_id, "parked-merge_gate")
        result["pushed"] = put_branch_file(
            env, branch, path, "# Notes\n\nText nobody reviewed.\n", "a push after review"
        )
        console().resolve(run_id, "merge", True, "e2e: approve the reviewed head")
        result["review"] = await_run(
            review_run_task(number), terminal=True, workflow="review"
        )
        result["story"] = audit_story(run_id)
        result["pr"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{number}"
        ).json()
        yield result
    finally:
        close_pr(env, result.get("pr_number") or 0)


def test_an_approval_of_a_moved_head_merges_nothing(moved_head: dict) -> None:
    """The verdict was recorded and released, the merge named the head the
    run reviewed, and the git host refused it: the run failed by name and
    the PR is refused — closed unmerged, at the commit nobody reviewed,
    with the host's refusal on it."""
    record = moved_head["review"]
    assert record["status"] == "failed", record
    assert "head moved after review" in (record["error"] or ""), record["error"]
    pr = moved_head["pr"]
    assert pr["merged"] is False and pr["state"] == "closed"
    assert pr["head"]["sha"] == moved_head["pushed"]
    story = moved_head["story"]
    gates = [e["rule"] for e in story if e["source"] == "process-gate"]
    assert gates[0] == "gate_parked" and "gate_approved" in gates, gates
    # Right after a push GitHub may first answer that the PR is not
    # mergeable yet; the retry then gets the refusal. Either way every
    # attempt was the platform's, and none landed.
    merges = [e for e in story if e["tool"] == "git.merge_pr"]
    assert merges and all(e["decision"] == "allow" for e in merges), merges
    # Nothing merged, so nothing the PR would fulfil was closed; the PR
    # itself was, with the refusal as its reason.
    assert not any(e["tool"] == "issues.close" for e in story)
    closes = [e for e in story if e["tool"] == "git.close_pr"]
    assert closes and "head moved after review" in (closes[-1]["rationale"] or "")


# -- how a merge gate ends ---------------------------------------------------
#
# One arc on a one-place company whose gates wait for the person: a parked
# review holds nothing, so the loop takes on the next ticket while it
# waits; a rejection refuses the delivery and its ticket comes back for
# another try; and approvals that land after another merge are verified
# on the moved base — a clean one merges, a conflicting one is refused and
# its ticket comes back.

REJECTION = "e2e: the slug must keep its unicode letters"


def _file(env: dict, nonce: str) -> int:
    return file_ticket(env, f"happy ({nonce})", ticket_body("happy", nonce))


def _delivered(number: int, since: str | None = None) -> int:
    """The loop's implement run on ticket `number` (born after `since`),
    to its end; the PR it delivered."""
    task = issue_run_task(number)
    wake_until_run_started(task, "implement", since=since)
    record = await_run(task, terminal=True, workflow="implement", since=since)
    assert record["status"] == "done" and record["pr_url"], record
    pr = pr_number(record["pr_url"])
    assert pr is not None, record
    return pr


def _parked(number: int) -> str:
    """The loop's review of PR `number`, parked on the person."""
    task = review_run_task(number)
    wake_until_run_started(task, "review")
    run_id = run_by_task(task, workflow="review")["run_id"]
    await_gate(run_id, "parked-merge_gate")
    return run_id


def _decided(number: int, run_id: str, approved: bool, why: str) -> dict:
    """The person's verdict on the parked review, and the run to its end."""
    console().resolve(run_id, "merge", approved, why)
    return await_run(review_run_task(number), terminal=True, workflow="review")


def _crew_pods(run_id: str) -> str:
    return kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "pods",
        "-l",
        f"poseidon.dev/kind=ephemeral-agent,poseidon.dev/run-id={run_id}",
        "-o",
        "jsonpath={.items[*].metadata.name}",
        timeout=30,
    )


@pytest.fixture(scope="module")
def gate_endings(stack: None, baseline_env: dict) -> dict:
    env = baseline_env
    apply_profile(
        may_start(HELLO_SAAS, "implement", "review"),
        env,
        gate_minutes=E2E_PATIENT_GATE_MINUTES,
    )
    nonce = secrets.token_hex(3)
    result: dict = {"env": env, "tickets": [], "prs": []}
    try:
        # A first delivery parks on the person, and gives back what it held.
        first = _file(env, f"{nonce}-a")
        result["tickets"].append(first)
        a1 = _delivered(first)
        result["prs"].append(a1)
        a1_review = _parked(a1)
        result["a1_review"] = a1_review
        result["parked"] = {
            "pods": _crew_pods(a1_review),
            "sandboxes": sandbox_pods(a1_review),
            "crew": [agent.status for agent in participants(a1_review)],
            "row": run_record(a1_review),
        }
        # The place it gave back: the loop takes on the next ticket.
        second = _file(env, f"{nonce}-b")
        result["tickets"].append(second)
        b1 = _delivered(second)
        result["prs"].append(b1)
        result["a1_state_while_b1_delivered"] = console().run(a1_review).execution.state
        b1_review = _parked(b1)

        # The person rejects the first: refused, and its ticket comes back
        # for another try the loop makes on its own.
        mark = db_now()
        result["a1_record"] = _decided(a1, a1_review, False, REJECTION)
        a2 = _delivered(first, since=mark)
        result["prs"].append(a2)
        result["a1"], result["a2"], result["b1"] = a1, a2, b1
        a2_review = _parked(a2)

        # The world opens a pull request that merges cleanly anywhere; a
        # person starts its review. Then the loop may implement no more, so
        # a ticket that comes back stays on the backlog to be read.
        w = open_world_pr(
            env,
            f"feature/notes-{nonce}",
            {f"docs/notes-{nonce}.md": "# Notes\n"},
            f"Add notes ({nonce})",
        )
        result["prs"].append(w)
        result["w"] = w
        w_review = console().start_run("review", w).run_id
        await_run(review_run_task(w), terminal=False, workflow="review")
        await_gate(w_review, "parked-merge_gate")
        apply_blueprint(may_start(HELLO_SAAS, "review"), E2E_PATIENT_GATE_MINUTES)
        wait_for_convergence()

        # Three approvals in turn: the first merges as reviewed; each later
        # one finds the base moved and is verified on it first.
        result["a2_record"] = _decided(a2, a2_review, True, "e2e: approve")
        result["a2_story"] = audit_story(a2_review)
        result["w_record"] = _decided(w, w_review, True, "e2e: approve")
        result["w_story"] = audit_story(w_review)
        result["b1_record"] = _decided(b1, b1_review, True, "e2e: approve")
        yield result
    finally:
        for number in result["tickets"]:
            close_issue(env, number)
        for number in result["prs"]:
            close_pr(env, number)
        restore_pristine()


def _comments(env: dict, number: int) -> list[str]:
    return [
        c["body"]
        for c in github_api(
            env, "GET", f"/repos/{target_slug(env)}/issues/{number}/comments"
        ).json()
    ]


def _pull(env: dict, number: int) -> dict:
    return github_api(env, "GET", f"/repos/{target_slug(env)}/pulls/{number}").json()


def _archives(story: list[dict]) -> list[dict]:
    """The trees the run built: the head, then one per merge verified onto
    a moved base."""
    return [e for e in story if e["tool"] == "repo.archive" and e["decision"] == "allow"]


def test_a_parked_review_holds_nothing(gate_endings: dict) -> None:
    """While the review waits on the person, its reviewer pod and sandbox
    are gone, its identities are revoked, and it holds no place under
    `max_concurrent_runs` — it is still running."""
    parked = gate_endings["parked"]
    assert parked["pods"] == "", parked["pods"]
    assert parked["sandboxes"] == [], parked["sandboxes"]
    assert parked["crew"] and all(s != "active" for s in parked["crew"]), parked["crew"]
    assert parked["row"]["status"] == "running" and parked["row"]["occupies"] is False


def test_a_one_place_company_works_while_a_review_waits(gate_endings: dict) -> None:
    """The second ticket was implemented while the first review was still
    parked: the place it gave back was taken."""
    assert gate_endings["a1_state_while_b1_delivered"] == "parked-merge_gate"


def test_a_rejection_refuses_the_delivery_and_its_ticket_comes_back(
    gate_endings: dict,
) -> None:
    """The person's rejection closes the pull request with their words on
    it; the ticket is told it is back with one attempt spent, and the loop
    implements it again on its own."""
    env, a1 = gate_endings["env"], gate_endings["a1"]
    record = gate_endings["a1_record"]
    assert record["status"] == "failed" and "merge gate rejected" in record["error"]
    pull = _pull(env, a1)
    assert pull["state"] == "closed" and pull["merged"] is False
    assert any(REJECTION in body for body in _comments(env, a1))
    first = gate_endings["tickets"][0]
    assert any(
        f"Pull request #{a1} was not merged" in body
        and "back on the backlog: attempt 1 of 2" in body
        for body in _comments(env, first)
    )
    attempts = runs_by_task(issue_run_task(first), workflow="implement")
    assert len(attempts) == 2, attempts
    assert attempts[0]["pr_settled"] == "refused"
    assert REJECTION in (attempts[0]["pr_settled_reason"] or "")


def test_the_first_approval_merges_as_reviewed(gate_endings: dict) -> None:
    """Nothing merged since its review: the redelivery merges with no
    verify on a moved base, and closes its ticket."""
    env = gate_endings["env"]
    assert gate_endings["a2_record"]["status"] == "done", gate_endings["a2_record"]
    assert _pull(env, gate_endings["a2"])["merged"] is True
    assert len(_archives(gate_endings["a2_story"])) == 1
    assert issue_state(env, gate_endings["tickets"][0]) == "closed"


def test_a_clean_merge_onto_a_moved_base_is_verified_then_merged(
    gate_endings: dict,
) -> None:
    """The base moved under the world's pull request: the platform built
    the head merged onto it — a second archive — ran the suite there, and
    merged on green."""
    env = gate_endings["env"]
    assert gate_endings["w_record"]["status"] == "done", gate_endings["w_record"]
    assert _pull(env, gate_endings["w"])["merged"] is True
    story = gate_endings["w_story"]
    assert len(_archives(story)) == 2, _archives(story)
    approved = next(i for i, e in enumerate(story) if e["rule"] == "gate_approved")
    verifies = [
        e
        for e in story[approved:]
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert verifies, "no verify of the merge onto the moved base"


def test_a_conflicting_merge_onto_a_moved_base_is_refused(gate_endings: dict) -> None:
    """The second delivery edits the same lines the first merged: it does
    not merge onto the moved base, the delivery is refused by name, and its
    ticket comes back — the loop may not implement here, so it waits."""
    env, b1 = gate_endings["env"], gate_endings["b1"]
    record = gate_endings["b1_record"]
    assert record["status"] == "failed" and "merge conflict" in record["error"], record
    pull = _pull(env, b1)
    assert pull["state"] == "closed" and pull["merged"] is False
    second = gate_endings["tickets"][1]
    assert issue_state(env, second) == "open"
    assert any(
        f"Pull request #{b1} was not merged: merge conflict" in body
        for body in _comments(env, second)
    )
