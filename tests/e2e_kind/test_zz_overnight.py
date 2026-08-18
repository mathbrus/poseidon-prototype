"""The overnight capstone: the company works while nobody watches.

This is the milestone's headline demo — the moment Poseidon stops being a
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

Named `test_zz_overnight` so it collects after `test_zz_acceptance`: it is
itself a capstone, and it adds runs the five-source assertion need not see.
The conducted blueprint (`examples/hello-saas-conducted.yaml`) is applied
for the module and the pristine one restored after.
"""

import asyncio
import contextlib
import json
import re
import subprocess
import time

import httpx
import pytest
from temporalio.client import Client

from common.ids import new_run_id
from orchestrator.workflows.conduct import ConductInput, conduct_workflow_id
from orchestrator.workflows.implement import FAST_TIMEOUTS
from orchestrator.workflows.intake import IntakeInput, intake_workflow_id
from orchestrator.workflows.review import review_workflow_id

from .conftest import (
    COMPANY_NS,
    PLATFORM_NS,
    POSTGRES_USER,
    REPO_ROOT,
    TASK_QUEUE,
    audit_story,
    temporal_port_forward,
)
from .test_operator_lifecycle import apply_blueprint, wait_for_convergence

CONDUCTED = REPO_ROOT / "examples" / "hello-saas-conducted.yaml"
HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"


def _psql(query: str) -> str:
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "exec",
            "-i",
            "statefulset/postgres",
            "--",
            "psql",
            "-U",
            POSTGRES_USER,
            "-d",
            POSTGRES_USER,
            "-At",
            "-c",
            query,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}"
    return proc.stdout.strip()


def _run_by_task(task: str) -> dict | None:
    row = _psql(
        "SELECT COALESCE(row_to_json(r), 'null') FROM registry.runs r "
        f"WHERE r.task = '{task}' LIMIT 1"
    )
    return json.loads(row) if row and row != "null" else None


def _await_run(task: str, terminal: bool, deadline_s: int = 1800) -> dict:
    """Poll the registry for a heartbeat-started run by its canonical task —
    optionally until it reaches a terminal state."""
    deadline = time.monotonic() + deadline_s
    while time.monotonic() < deadline:
        run = _run_by_task(task)
        if run and (not terminal or run["status"] in ("done", "failed")):
            return run
        time.sleep(5)
    raise AssertionError(f"run {task!r} never {'finished' if terminal else 'started'}")


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


def _pr_number(pr_url: str) -> int:
    match = re.search(r"/pull/(\d+)$", pr_url or "")
    assert match, f"not a PR url: {pr_url!r}"
    return int(match.group(1))


@pytest.fixture(scope="module")
def overnight(stack: None, env: dict) -> dict:
    """Drive the whole unattended arc and hand back every artifact for the
    assertions to read."""
    apply_blueprint(CONDUCTED)
    wait_for_convergence()

    intake_run = new_run_id()
    nonce = intake_run[-6:].lower()
    # The goal carries the delivery branch, so the PM's ticket carries it,
    # so the heartbeat's implement run delivers on a fresh branch — reruns
    # against the disposable repo never collide.
    goal = (
        f"Ship the slugify utility (e2e overnight goal {nonce})\n\n"
        f"Deliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    heartbeat_id = f"{conduct_workflow_id(COMPANY_NS)}-overnight-{nonce}"
    result: dict = {"env": env, "heartbeat_id": heartbeat_id}

    async def drive() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)

            # --- human act 1: one goal in --------------------------------
            intake_handle = await client.start_workflow(
                "intake",
                IntakeInput(run_id=intake_run, goal_text=goal, timeouts=FAST_TIMEOUTS),
                id=intake_workflow_id(intake_run),
                task_queue=TASK_QUEUE,
            )
            result["intake"] = await asyncio.wait_for(intake_handle.result(), timeout=600)
            ticket_urls = result["intake"].get("ticket_urls") or []
            issue_match = next(
                (re.search(r"/issues/(\d+)$", u) for u in ticket_urls if u), None
            )
            assert issue_match, f"intake filed no issue: {ticket_urls}"
            issue_number = int(issue_match.group(1))
            result["issue_number"] = issue_number

            # --- touch nothing: the heartbeat runs the company -----------
            beat = await client.start_workflow(
                "conduct",
                ConductInput(
                    company=COMPANY_NS, tick_interval_s=3, timeouts=FAST_TIMEOUTS
                ),
                id=heartbeat_id,
                task_queue=TASK_QUEUE,
            )
            try:
                implement = await asyncio.to_thread(
                    _await_run, f"implement issue #{issue_number}", True
                )
                assert implement["status"] == "done", f"unattended implement: {implement}"
                assert implement["pr_url"], "the unattended implement produced no PR"
                result["implement"] = implement
                pr_number = _pr_number(implement["pr_url"])
                result["pr_number"] = pr_number

                # The heartbeat sees the delivered PR and starts a review,
                # which parks on the human merge gate.
                review = await asyncio.to_thread(
                    _await_run, f"review pull request #{pr_number}", False
                )
                result["review"] = review
                review_handle = client.get_workflow_handle(
                    review_workflow_id(review["run_id"])
                )
                deadline = time.monotonic() + 600
                while time.monotonic() < deadline:
                    if await review_handle.query("state") == "paused-merge_gate":
                        break
                    await asyncio.sleep(2)
                else:
                    raise AssertionError("the unattended review never parked to merge")
                result["parked_pr"] = _github(
                    env,
                    "GET",
                    f"/repos/{'/'.join(env['TARGET_REPO'].split('/')[-2:])}/pulls/{pr_number}",
                ).json()

                # --- human act 2: one approval out -----------------------
                await review_handle.signal("merge", args=[True, "overnight: approved"])
                result["review_record"] = await asyncio.wait_for(
                    review_handle.result(), timeout=300
                )
            finally:
                with contextlib.suppress(Exception):
                    await beat.terminate()

    asyncio.run(drive())

    yield result

    slug = "/".join(env["TARGET_REPO"].split("/")[-2:])
    with contextlib.suppress(Exception):
        _github(
            env,
            "PATCH",
            f"/repos/{slug}/issues/{result.get('issue_number')}",
            json={"state": "closed"},
        )
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()


def test_the_company_implemented_its_own_ticket_unattended(overnight: dict):
    """No human ran the implement: the heartbeat consumed the intake ticket
    and produced a PR, under an auto-minted run id."""
    implement = overnight["implement"]
    assert implement["run_id"].startswith("auto-"), implement["run_id"]
    assert implement["workflow"] == "implement"
    assert implement["status"] == "done"
    assert implement["pr_url"]
    # The decision that started it is the heartbeat's, journaled.
    beat = audit_story(overnight["heartbeat_id"])
    assert any(e["tool"] == "conduct.decide" for e in beat)
    assert any(e["tool"] == "conduct.start" and e["decision"] == "allow" for e in beat)


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
    pull = _github(
        overnight["env"],
        "GET",
        f"/repos/{'/'.join(overnight['env']['TARGET_REPO'].split('/')[-2:])}/pulls/{overnight['pr_number']}",
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
