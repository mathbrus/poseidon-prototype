"""The raw road releases nothing.

The fence closes the in-cluster road to the Temporal frontend; this
module walks the one that stays open — a port-forward with cluster
credentials, beneath any policy — and proves it is harmless: a `merge`
signal delivered straight to the workflow engine finds no verdict the
console recorded, `merge.resolve` refuses it by name, the run ends failed
and the pull request stays unmerged. Then the same verdict through the
console, on the review of a second PR, merges — and the story names the
person.

Two PRs the company delivers under an implement-only profile, then the
review-only profile applied over them without a sweep — the two open PRs
are the backlog this module means the loop to have. A review that
reached a verdict concluded its PR, so the loop reviews each once: the
review the raw road failed is never retried into the lane the console's
review needs.
"""

import asyncio

import pytest
from temporalio.client import Client

from common.tasks import review_run_task
from common.workflows import workflow_id

from .blueprints import (
    HELLO_SAAS,
    apply_blueprint,
    may_start,
    restore_pristine,
    wait_for_convergence,
)
from .cluster import (
    HUMAN,
    audit_story,
    await_bare_heartbeat_asleep,
    await_run,
    console,
    run_by_task,
    temporal_port_forward,
)
from .conftest import (
    apply_profile,
    await_gate,
    close_issue,
    close_pr,
    file_ticket,
    github_api,
    pr_number,
    run_ticket,
    target_slug,
    ticket_body,
    wake_until_run_started,
)


def _raw_signal(run_id: str) -> None:
    """The road the fence cannot close: a `merge` signal delivered to the
    workflow engine over a port-forward, as anyone with cluster
    credentials could — and as nobody should."""

    async def send() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(workflow_id("review", run_id))
            await handle.signal("merge", args=[True, "red-team: past the console"])

    asyncio.run(send())


def _pull(env: dict, number: int) -> dict:
    return github_api(env, "GET", f"/repos/{target_slug(env)}/pulls/{number}").json()


@pytest.fixture(scope="module")
def delivered(stack: None, baseline_env: dict) -> dict:
    """Two PRs the company delivered under an implement-only profile, for
    two reviews — the raw road's and the console's — under the review-only
    one, applied over the open PRs on purpose: they are the backlog."""
    env = baseline_env
    import secrets

    apply_profile(may_start(HELLO_SAAS, "implement"), env)
    prs: list[int] = []
    issues: list[int] = []
    for _ in range(2):
        nonce = secrets.token_hex(3)
        number = file_ticket(env, f"happy ({nonce})", ticket_body("happy", nonce))
        issues.append(number)
        record = run_ticket(number)
        assert record["status"] == "done" and record["pr_url"], record
        prs.append(pr_number(record["pr_url"]))
    apply_blueprint(may_start(HELLO_SAAS, "review"))
    wait_for_convergence()
    await_bare_heartbeat_asleep()
    try:
        yield {"env": env, "prs": prs}
    finally:
        for number in prs:
            close_pr(env, number)
        for number in issues:
            close_issue(env, number)
        restore_pristine()


@pytest.fixture(scope="module")
def raw_review(delivered: dict) -> dict:
    """The first review: parked on the merge gate, then answered past the
    console."""
    number = delivered["prs"][0]
    task = review_run_task(number)
    wake_until_run_started(task, "review")
    run_id = run_by_task(task, workflow="review")["run_id"]
    await_gate(run_id, "paused-merge_gate")
    _raw_signal(run_id)
    record = await_run(task, terminal=True, deadline_s=600, workflow="review")
    return {"run_id": run_id, "record": record, "pr_number": number}


def test_a_signal_past_the_console_releases_nothing(raw_review: dict, delivered) -> None:
    """The workflow took the signal as a nudge, read the audit store for
    the verdict, found none, and refused by name: no merge, no
    `git.merge_pr` call, the PR still open."""
    record = raw_review["record"]
    assert record["status"] == "failed", record
    assert "no verified human verdict" in (record["error"] or "")
    assert "merge_resolve_no_human_verdict" in (record["error"] or "")
    story = audit_story(raw_review["run_id"])
    gate_rules = [e["rule"] for e in story if e["source"] == "process-gate"]
    assert gate_rules[-2:] == ["gate_approved", "gate_verdict_unverified"], gate_rules
    resolves = [e for e in story if e["tool"] == "merge.resolve"]
    assert resolves and resolves[-1]["decision"] == "deny"
    assert resolves[-1]["rule"] == "merge_resolve_no_human_verdict"
    assert not any(e["tool"] == "git.merge_pr" for e in story), "a merge was attempted"
    assert not any(e["source"] == "console" for e in story), "the console never spoke"
    pull = _pull(delivered["env"], raw_review["pr_number"])
    assert pull["state"] == "open" and pull["merged"] is False


def test_the_same_verdict_through_the_console_merges(raw_review: dict, delivered) -> None:
    """The second PR's review, answered through the console: recorded
    under the human's name, released by policy on its human path,
    merged."""
    env = delivered["env"]
    number = delivered["prs"][1]
    task = review_run_task(number)
    wake_until_run_started(task, "review")
    run_id = run_by_task(task, workflow="review")["run_id"]
    assert run_id != raw_review["run_id"]
    await_gate(run_id, "paused-merge_gate")
    verdict = console(env).resolve(run_id, "merge", True, "red-team: the real road")
    assert verdict["principal"] == HUMAN
    record = await_run(task, terminal=True, deadline_s=600, workflow="review")
    assert record["status"] == "done", record

    story = audit_story(run_id)
    recorded = [
        e for e in story if e["source"] == "console" and e["tool"] == "gate.resolve"
    ]
    assert [(e["agent_id"], e["rule"]) for e in recorded] == [
        (HUMAN, "gate_resolve_merge_approve")
    ]
    release = [e for e in story if e["tool"] == "merge.resolve"][-1]
    assert (release["decision"], release["rule"]) == ("allow", "merge_resolve_human")
    assert any(e["tool"] == "git.merge_pr" and e["decision"] == "allow" for e in story)
    assert _pull(env, number)["merged"] is True
