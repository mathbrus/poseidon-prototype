"""The overnight capstone: the company works while nobody watches.

This is the headline demo — the moment Poseidon stops being a
pipeline you operate and becomes a company you supervise. The whole run has
exactly two human acts:

  1. `poseidon run <goal> --workflow intake` — one goal in.
  2. `poseidon signal <run> merge --approved` — one approval out.

Everything between is the standing heartbeat's: it reads the ticket intake
filed, implements it (a PR appears), sees the delivered-but-unreviewed PR,
reviews it, and parks that review on the human merge gate — all under the
declared caps, with no command starting the next step. Then the single
human verdict lands and the gateway merges.

The concurrency cap does real sequencing work here: `max_concurrent_runs:
1` is why the review waits for the implement run to finish rather than
racing it, and why the parked review blocks any further starts — the
company does one thing at a time because the blueprint said so.

The conftest orders this module after `test_acceptance`: it is itself a
capstone, and it adds runs the five-source assertion need not see. The
heartbeat blueprint (`examples/hello-saas-heartbeat.yaml`) is applied
for the module and the pristine one restored after.
"""

import asyncio
import json
import os
import re
import subprocess
import tempfile
import time
from pathlib import Path

import pytest
from temporalio.client import Client

from common.ids import new_run_id
from orchestrator.workflows.base import workflow_id

from .blueprints import HEARTBEAT, restore_pristine
from .cluster import COMPANY_NS, audit_story, await_run, temporal_port_forward
from .conftest import (
    REPO_ROOT,
    apply_profile,
    close_issue,
    close_pr,
    deployed_timeouts,
    github_api,
    heartbeat_ticks,
    pr_number,
    target_slug,
    wake_until_run_started,
)


def _poseidon(args: list[str], address: str, timeout: int) -> subprocess.CompletedProcess:
    """`poseidon …` as the human runs it, pointed at the cluster's Temporal
    through an open port-forward — the CLI the two human acts go through."""
    return subprocess.run(
        ["uv", "run", "poseidon", *args],
        cwd=REPO_ROOT,
        env={**os.environ, "TEMPORAL_ADDRESS": address},
        capture_output=True,
        text=True,
        timeout=timeout,
    )


def _run_intake(goal_path: Path, run_id: str, address: str) -> dict:
    """Human act 1: `poseidon run <goal> --workflow intake`. Blocks until
    intake completes and prints the run record; returns it."""
    fast = ["--fast-timeouts"] if deployed_timeouts() is not None else []
    proc = _poseidon(
        ["run", str(goal_path), "--workflow", "intake", "--run-id", run_id, *fast],
        address,
        650,
    )
    assert proc.returncode == 0, f"poseidon run intake: {proc.stderr}\n{proc.stdout}"
    return json.loads(proc.stdout)


def _signal_merge(run_id: str, address: str) -> None:
    """Human act 2: `poseidon signal <run> merge --approved --workflow review`."""
    proc = _poseidon(
        ["signal", run_id, "merge", "--approved", "--workflow", "review"], address, 60
    )
    assert proc.returncode == 0, f"poseidon signal merge: {proc.stderr}"


@pytest.fixture(scope="module")
def overnight(stack: None, env: dict) -> dict:
    """Drive the whole unattended arc and hand back every artifact for the
    assertions to read."""
    apply_profile(HEARTBEAT, env)

    intake_run = new_run_id()
    nonce = intake_run[-6:].lower()
    # The goal carries the delivery branch, so the PM's ticket carries it,
    # so the heartbeat's implement run delivers on a fresh branch — reruns
    # against the disposable repo never collide.
    goal = (
        f"Ship the slugify utility (e2e overnight goal {nonce})\n\n"
        f"Deliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    goal_path = Path(tempfile.mkdtemp(prefix="poseidon-goal-")) / "overnight-goal.md"
    goal_path.write_text(goal)
    heartbeat_id = workflow_id("heartbeat", COMPANY_NS)
    result: dict = {"env": env, "heartbeat_id": heartbeat_id}

    async def drive() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)

            # --- human act 1: one goal in, through the real CLI ----------
            result["intake"] = await asyncio.to_thread(
                _run_intake, goal_path, intake_run, address
            )
            ticket_urls = result["intake"].get("ticket_urls") or []
            issue_match = next(
                (re.search(r"/issues/(\d+)$", u) for u in ticket_urls if u), None
            )
            assert issue_match, f"intake filed no issue: {ticket_urls}"
            issue_number = int(issue_match.group(1))
            result["issue_number"] = issue_number

            # --- touch nothing: the standing heartbeat runs the company —
            # the intake's finishing nudge wakes it onto the ticket it filed,
            # and the re-nudge below covers a backlog read that landed before
            # the ticket was visible on the git host.
            await asyncio.to_thread(
                wake_until_run_started,
                f"implement issue #{issue_number}",
                "implement",
            )
            implement = await asyncio.to_thread(
                await_run, f"implement issue #{issue_number}", True
            )
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
                wake_until_run_started, f"review pull request #{number}", "review"
            )
            review = await asyncio.to_thread(
                await_run, f"review pull request #{number}", False
            )
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

            # --- human act 2: one approval out, through the real CLI -----
            await asyncio.to_thread(_signal_merge, review["run_id"], address)
            result["review_record"] = await asyncio.wait_for(
                review_handle.result(), timeout=300
            )

    asyncio.run(drive())

    yield result

    close_pr(env, result.get("pr_number") or 0)
    close_issue(env, result.get("issue_number"))
    restore_pristine()


def test_the_company_implemented_its_own_ticket_unattended(overnight: dict):
    """No human ran the implement: the heartbeat consumed the intake ticket
    and produced a PR, under an auto-minted run id. Asserted on the run and
    on the heartbeat's own ticks — the started tick names the child run."""
    implement = overnight["implement"]
    assert implement["run_id"].startswith("auto-"), implement["run_id"]
    assert implement["workflow"] == "implement"
    assert implement["status"] == "done"
    assert implement["pr_url"]
    # The authorized start is journaled under the run it started — the
    # compiled heartbeat.start decision attributes to the child, whichever
    # heartbeat asked.
    story = audit_story(implement["run_id"])
    assert any(e["tool"] == "heartbeat.start" and e["decision"] == "allow" for e in story)
    # And the standing heartbeat recorded starting it: the tick that
    # names the child run carries the writing workflow's own id.
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


def test_the_whole_arc_had_exactly_two_human_acts(overnight: dict):
    """The claim stated as an invariant: implement and review each ran
    under an auto- run id (the heartbeat's), and the merge only happened
    after the human signal. The two human touches were the intake start
    and the merge approval — nothing started the implement or the review."""
    assert overnight["implement"]["run_id"].startswith("auto-")
    assert overnight["review"]["run_id"].startswith("auto-")
    merges = [
        e
        for e in audit_story(overnight["review"]["run_id"])
        if e["tool"] == "git.merge_pr"
    ]
    assert merges, "no merge happened for the reviewed PR"
    gate = [
        e
        for e in audit_story(overnight["review"]["run_id"])
        if e["source"] == "process-gate" and e["rule"] == "gate_approved"
    ]
    assert gate, "the merge was not released by an approved gate"
