"""The raw road releases nothing.

The fence closes the in-cluster road to the Temporal frontend; this
module walks the one that stays open — a port-forward with cluster
credentials, beneath any policy — and proves it is harmless: a `merge`
signal delivered straight to the workflow engine finds no verdict the
console recorded, `merge.resolve` refuses it by name, the run ends failed
and the pull request is refused: closed, never merged. The same verdict
through the console merges and names the person: that is
`test_review_gate`'s arc.

The pull request is one the world opened, reviewed by a person's start
under the pristine blueprint — the heartbeat observes only the PRs the
company delivered, so nothing else touches it.
"""

import asyncio
import secrets

import pytest
from temporalio.client import Client

from common.policy import MERGE_RESOLVE, RUN_START
from common.tasks import review_run_task
from common.workflows import workflow_id

from .cluster import (
    audit_story,
    await_run,
    console,
    temporal_port_forward,
)
from .conftest import (
    await_gate,
    close_pr,
    github_api,
    open_world_pr,
    target_slug,
)


def _raw_signal(run_id: str) -> None:
    """The road the fence cannot close: a `merge` signal delivered to the
    workflow engine over a port-forward, as anyone with cluster
    credentials could — and as nobody should."""

    async def send() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(workflow_id("review", run_id))
            await handle.signal("merge", args=[True])

    asyncio.run(send())


@pytest.fixture(scope="module")
def raw_review(stack: None, baseline_env: dict) -> dict:
    """A review a person started on a pull request the world opened,
    parked on the merge gate, then answered past the console."""
    env = baseline_env
    nonce = secrets.token_hex(3)
    result: dict = {"env": env}
    try:
        number = open_world_pr(
            env,
            f"feature/raw-{nonce}",
            {f"docs/raw-{nonce}.md": "# Notes\n"},
            f"Add notes ({nonce})",
        )
        result["pr_number"] = number
        run_id = console().start_run("review", number).run_id
        result["run_id"] = run_id
        task = review_run_task(number)
        # The workflow writes the run's row; the console knows the run
        # only from then on.
        await_run(task, terminal=False, workflow="review")
        await_gate(run_id, "parked-merge_gate")
        _raw_signal(run_id)
        result["record"] = await_run(
            task, terminal=True, deadline_s=600, workflow="review"
        )
        result["pull"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{number}"
        ).json()
        yield result
    finally:
        close_pr(env, result.get("pr_number") or 0)


def test_a_signal_past_the_console_releases_nothing(raw_review: dict) -> None:
    """The workflow took the signal as a nudge, read the audit store for
    the verdict, found none, and refused by name: no merge, no
    `git.merge_pr` call, and the delivery refused — the PR closed with
    the reason on it, never merged."""
    record = raw_review["record"]
    assert record["status"] == "failed", record
    assert "no verified human verdict" in (record["error"] or "")
    assert "merge_resolve_no_human_verdict" in (record["error"] or "")
    story = audit_story(raw_review["run_id"])
    gate_rules = [e["rule"] for e in story if e["source"] == "process-gate"]
    assert gate_rules[-2:] == ["gate_approved", "gate_verdict_unverified"], gate_rules
    resolves = [e for e in story if e["tool"] == MERGE_RESOLVE]
    assert resolves and resolves[-1]["decision"] == "deny"
    assert resolves[-1]["rule"] == "merge_resolve_no_human_verdict"
    assert not any(e["tool"] == "git.merge_pr" for e in story), "a merge was attempted"
    # The person who started the review spoke to the console; nobody
    # answered the gate there.
    assert not any(e["source"] == "console" and e["tool"] != RUN_START for e in story), (
        "the console answered the gate"
    )
    closes = [e for e in story if e["tool"] == "git.close_pr"]
    assert closes and closes[-1]["decision"] == "allow", closes
    assert "no verified human verdict" in (closes[-1]["rationale"] or "")
    pull = raw_review["pull"]
    assert pull["state"] == "closed" and pull["merged"] is False
