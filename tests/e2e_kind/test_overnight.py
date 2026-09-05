"""The overnight capstone: the company works while nobody watches.

This is the headline demo — the moment Poseidon stops being a pipeline
you operate and becomes a company you supervise. Nothing goes in: the
company's strategy names the next slice, and the whole run has exactly
one human act:

  1. `poseidon signal <run> merge --approved` — one approval out.

Everything before is the standing heartbeat's: it reads the documents,
scopes the slice into a ticket, implements it (a PR appears), sees the
delivered-but-unreviewed PR, reviews it, and parks that review on the
human merge gate — all under the declared caps, with no command starting
any step. Then the single human verdict lands and the gateway merges.

The concurrency cap does real sequencing work here: `max_concurrent_runs:
1` is why the review waits for the implement run to finish rather than
racing it, and why the parked review blocks any further starts — the
company does one thing at a time because the blueprint said so.

The conftest orders this module after `test_acceptance`: it is itself a
capstone, and it adds runs the five-source assertion need not see. The
example as shipped (`hello-saas.yaml`, every workflow startable) is
applied for the module and the pristine profile restored after.
"""

import asyncio
import os
import re
import secrets
import subprocess
import time

import pytest
from temporalio.client import Client

from common.tasks import review_run_task
from mockllm.brain import NEXT_SLICE_MARKER
from orchestrator.workflows.base import workflow_id

from .blueprints import HELLO_SAAS, restore_pristine
from .cluster import (
    audit_story,
    await_run,
    bare_heartbeat_id,
    heartbeat_ticks,
    temporal_port_forward,
)
from .conftest import (
    REPO_ROOT,
    apply_profile,
    close_issue,
    close_pr,
    github_api,
    open_slice_strategy,
    pr_number,
    restore_strategy,
    run_ticket,
    scope_via_heartbeat,
    target_slug,
    wake_until_run_started,
)


def _signal_merge(run_id: str, address: str) -> None:
    """The one human act: `poseidon signal <run> merge --approved
    --workflow review`, through the real CLI pointed at the cluster's
    Temporal over an open port-forward."""
    proc = subprocess.run(
        [
            "uv",
            "run",
            "poseidon",
            "signal",
            run_id,
            "merge",
            "--approved",
            "--workflow",
            "review",
        ],
        cwd=REPO_ROOT,
        env={**os.environ, "TEMPORAL_ADDRESS": address},
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"poseidon signal merge: {proc.stderr}"


@pytest.fixture(scope="module")
def overnight(stack: None, baseline_env: dict) -> dict:
    """Drive the whole unattended arc and hand back every artifact for the
    assertions to read."""
    env = baseline_env
    apply_profile(HELLO_SAAS, env)
    nonce = secrets.token_hex(3)
    result: dict = {"env": env, "heartbeat_id": bare_heartbeat_id()}

    async def drive() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)

            # --- nothing in: the strategy names the slice, the loop scopes
            # it. The wake stands in for the reconcile look the loop would
            # take on its own.
            scope = await asyncio.to_thread(
                scope_via_heartbeat, env, open_slice_strategy(nonce, NEXT_SLICE_MARKER)
            )
            assert scope["status"] == "done", f"unattended scope: {scope}"
            result["scope"] = scope
            urls = scope.get("ticket_urls") or []
            issue_match = next((re.search(r"/issues/(\d+)$", u) for u in urls if u), None)
            assert issue_match, f"the scope run filed no issue: {urls}"
            issue_number = int(issue_match.group(1))
            result["issue_number"] = issue_number

            # --- touch nothing: the scope run's finishing nudge wakes the
            # loop onto the ticket it filed; the re-nudge covers a backlog
            # read that landed before the ticket was visible on the git host.
            implement = await asyncio.to_thread(run_ticket, issue_number)
            assert implement["status"] == "done", f"unattended implement: {implement}"
            assert implement["pr_url"], "the unattended implement produced no PR"
            result["implement"] = implement
            number = pr_number(implement["pr_url"])
            assert number is not None, f"not a PR url: {implement['pr_url']!r}"
            result["pr_number"] = number

            # The implement's finishing nudge wakes the heartbeat onto
            # the delivered PR; the review it starts parks on the human
            # merge gate.
            await asyncio.to_thread(
                wake_until_run_started, review_run_task(number), "review"
            )
            review = await asyncio.to_thread(await_run, review_run_task(number), False)
            result["review"] = review
            review_handle = client.get_workflow_handle(
                workflow_id("review", review["run_id"])
            )
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                if await review_handle.query("state") == "paused-merge_gate":
                    break
                await asyncio.sleep(2)
            else:
                raise AssertionError("the unattended review never parked to merge")
            result["parked_pr"] = github_api(
                env, "GET", f"/repos/{target_slug(env)}/pulls/{number}"
            ).json()

            # --- the one human act: one approval out, through the real CLI
            await asyncio.to_thread(_signal_merge, review["run_id"], address)
            result["review_record"] = await asyncio.wait_for(
                review_handle.result(), timeout=300
            )

    try:
        asyncio.run(drive())
        yield result
    finally:
        close_pr(env, result.get("pr_number") or 0)
        close_issue(env, result.get("issue_number"))
        restore_strategy(env)
        restore_pristine()


def test_the_company_scoped_its_own_slice_unattended(overnight: dict):
    """No human filed the ticket: the heartbeat read the strategy, started a
    scope run under an auto-minted id, and the PM filed the slice."""
    scope = overnight["scope"]
    assert scope["run_id"].startswith("auto-"), scope["run_id"]
    assert scope["workflow"] == "scope" and scope["ticket_urls"]
    started = [
        t
        for t in heartbeat_ticks(overnight["heartbeat_id"])
        if t["decision"] == "allow" and scope["run_id"] in (t["rule"] or "")
    ]
    assert started, "this heartbeat never recorded starting the scope run"


def test_the_company_implemented_its_own_ticket_unattended(overnight: dict):
    """No human ran the implement: the heartbeat consumed the ticket the
    scope run filed and produced a PR, under an auto-minted run id.
    Asserted on the run and on the heartbeat's own ticks — the started
    tick names the child run."""
    implement = overnight["implement"]
    assert implement["run_id"].startswith("auto-"), implement["run_id"]
    assert implement["workflow"] == "implement"
    assert implement["status"] == "done"
    assert implement["pr_url"]
    story = audit_story(implement["run_id"])
    assert any(e["tool"] == "heartbeat.start" and e["decision"] == "allow" for e in story)
    started = [
        t
        for t in heartbeat_ticks(overnight["heartbeat_id"])
        if t["decision"] == "allow" and implement["run_id"] in (t["rule"] or "")
    ]
    assert started, "this heartbeat never recorded starting the implement"


def test_the_company_reviewed_its_own_pr_unattended(overnight: dict):
    """Also no human: the heartbeat saw the delivered-but-unreviewed PR and
    started the review that parked on the gate."""
    review = overnight["review"]
    assert review["run_id"].startswith("auto-"), review["run_id"]
    assert review["workflow"] == "review"
    assert review["run_id"] != overnight["implement"]["run_id"]


def test_nothing_merged_before_the_human_verdict(overnight: dict):
    """The parked PR — captured at the moment the gate blocked — was open,
    unmerged. The autonomy stops exactly at the merge."""
    assert overnight["parked_pr"]["merged"] is False


def test_the_single_human_verdict_completes_delivery(overnight: dict):
    """One approval out: the merge signal releases the gateway merge, the
    review run finishes, and the PR is merged."""
    assert overnight["review_record"]["status"] == "done", overnight["review_record"]
    env = overnight["env"]
    pull = github_api(
        env, "GET", f"/repos/{target_slug(env)}/pulls/{overnight['pr_number']}"
    ).json()
    assert pull["merged"] is True, "the approved PR was not merged"


def test_the_whole_arc_had_exactly_one_human_act(overnight: dict):
    """The claim stated as an invariant: scope, implement and review each
    ran under an auto- run id (the heartbeat's), and the merge only
    happened after the human signal. The one human touch was the merge
    approval — nothing started anything."""
    for key in ("scope", "implement", "review"):
        assert overnight[key]["run_id"].startswith("auto-"), key
    story = audit_story(overnight["review"]["run_id"])
    assert any(e["tool"] == "git.merge_pr" for e in story), "no merge happened"
    assert any(
        e["source"] == "process-gate" and e["rule"] == "gate_approved" for e in story
    ), "the merge was not released by an approved gate"
