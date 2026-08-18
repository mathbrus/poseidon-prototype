"""The delegated capstone: the company delivers its own work — merge and
all — with zero human commands.

The overnight capstone (`test_zz_overnight`) proved the company runs its
backlog to the human merge gate: two human acts, one goal in and one
approval out. This is the milestone (#11) that removes the second act for a
declared class of work. Under a policy-resolved merge gate, the whole arc
has exactly *one* human act:

  1. `poseidon run <goal> --workflow intake` — one goal in.

Everything after is the standing heartbeat's: it implements the ticket
intake filed (a PR appears), reviews the delivered PR, and — because the
change is in the delegated class, the independent review passed, and the
platform's own checks are green — merges it. No approval, no signal, no
command. The autonomy now runs through the merge.

What the platform still refuses is proven where it is exact and cheap, not
by paying for more unattended cluster runs here:

- a carve-out PR parks on the human `merge` signal even with a perfect
  review — `test_merge_gate_redteam` drives that on the cluster, and
  `test_review_workflow` at the workflow layer;
- a failed quality layer feeds the bounded rework loop and, at the bound,
  escalates to the human — `test_review_workflow` (exhausted chain) and
  `test_review_activities` (depth-from-lineage) prove it exactly.

So this capstone pins the one thing only a full unattended cluster run can
show: passing in-class work reaches `main` with no human after the goal.

Named `test_zz_delegated` so it collects after the other capstones and adds
its runs last. Applies `examples/hello-saas-delegated.yaml` (heartbeat +
delegated merge) for the module and restores the pristine `hello-saas.yaml`
after.
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

DELEGATED = REPO_ROOT / "examples" / "hello-saas-delegated.yaml"
HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"


def _psql(query: str) -> str:
    proc = subprocess.run(
        [
            "kubectl", "-n", PLATFORM_NS, "exec", "-i", "statefulset/postgres",
            "--", "psql", "-U", POSTGRES_USER, "-d", POSTGRES_USER, "-At", "-c", query,
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


def _slug(env: dict) -> str:
    return "/".join(env["TARGET_REPO"].split("/")[-2:])


def _pr_number(pr_url: str) -> int:
    match = re.search(r"/pull/(\d+)$", pr_url or "")
    assert match, f"not a PR url: {pr_url!r}"
    return int(match.group(1))


@pytest.fixture(scope="module")
def delegated_overnight(stack: None, env: dict) -> dict:
    """One goal in, then nothing: drive intake, start the heartbeat, and
    let the company implement, review, and MERGE its own in-class PR with
    no further command. Hand back every artifact for the assertions."""
    apply_blueprint(DELEGATED)
    wait_for_convergence()

    intake_run = new_run_id()
    nonce = intake_run[-6:].lower()
    goal = (
        f"Ship the slugify utility (e2e delegated goal {nonce})\n\n"
        f"Deliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    heartbeat_id = f"{conduct_workflow_id(COMPANY_NS)}-delegated-{nonce}"
    result: dict = {"env": env, "heartbeat_id": heartbeat_id}

    async def drive() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)

            # --- the one human act: one goal in --------------------------
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

            # --- touch nothing more: the heartbeat runs the whole arc ----
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

                # The heartbeat sees the delivered PR, reviews it, and —
                # in-class, review passed, checks green — MERGES it. No
                # human signal is ever sent.
                review = await asyncio.to_thread(
                    _await_run, f"review pull request #{pr_number}", True
                )
                result["review"] = review
            finally:
                with contextlib.suppress(Exception):
                    await beat.terminate()

    asyncio.run(drive())

    # The merged PR, read after the arc completed.
    result["merged_pr"] = _github(
        env, "GET", f"/repos/{_slug(env)}/pulls/{result['pr_number']}"
    ).json()

    yield result

    with contextlib.suppress(Exception):
        _github(
            env,
            "PATCH",
            f"/repos/{_slug(env)}/issues/{result.get('issue_number')}",
            json={"state": "closed"},
        )
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()


def test_the_company_implemented_and_reviewed_unattended(delegated_overnight: dict):
    """Both runs are the heartbeat's — auto-minted run ids, no human
    command started either."""
    implement = delegated_overnight["implement"]
    review = delegated_overnight["review"]
    assert implement["run_id"].startswith("auto-"), implement["run_id"]
    assert review["run_id"].startswith("auto-"), review["run_id"]
    assert review["run_id"] != implement["run_id"]
    assert review["workflow"] == "review"


def test_the_in_class_pr_merged_with_no_human_command(delegated_overnight: dict):
    """The headline: the PR is merged on GitHub, and no human ever touched
    it after the goal. The review run finished done and the PR is merged."""
    assert delegated_overnight["review"]["status"] == "done", (
        delegated_overnight["review"]
    )
    assert delegated_overnight["merged_pr"]["merged"] is True, (
        "the in-class PR did not merge unattended"
    )


def test_the_merge_was_released_by_policy_not_a_human(delegated_overnight: dict):
    """The merge was released by the compiled decision, not a human gate.
    The review's story carries a policy_resolved gate outcome and a
    merge.resolve allow — and NO human gate_approved event."""
    story = audit_story(delegated_overnight["review"]["run_id"])
    gate_rules = [e["rule"] for e in story if e["tool"] == "gate.merge"]
    assert "gate_policy_resolved" in gate_rules, gate_rules
    assert "gate_approved" not in gate_rules, "a human approval sneaked in"
    resolves = [e for e in story if e["tool"] == "merge.resolve"]
    assert resolves and resolves[-1]["decision"] == "allow", resolves
    assert resolves[-1]["rule"] == "merge_resolve_delegated"
    # And the merge really happened as a platform tool call.
    assert any(e["tool"] == "git.merge_pr" for e in story), "no merge tool call"


def test_both_quality_layers_were_recorded(delegated_overnight: dict):
    """The merge did not skip the layers: the reviewer's verdict was
    journaled (allow) and the platform's own decision saw the real facts —
    the run_id ties findings, verdict, checks and merge into one story."""
    story = audit_story(delegated_overnight["review"]["run_id"])
    verdicts = [e for e in story if e["tool"] == "review.verdict"]
    assert verdicts and verdicts[-1]["decision"] == "allow", verdicts
    # The decision that started the review is the heartbeat's, journaled
    # under this run's own id (conduct.start attributes to the child it
    # authorizes) — so one run_id query ties the whole arc together.
    assert any(
        e["tool"] == "conduct.start" and e["decision"] == "allow" for e in story
    )


def test_the_whole_arc_had_exactly_one_human_act(delegated_overnight: dict):
    """The milestone claim, as an invariant: implement and review both ran
    under auto- ids, the merge happened, and it was released by policy —
    never by a human gate signal. The only human act was the intake goal."""
    assert delegated_overnight["implement"]["run_id"].startswith("auto-")
    assert delegated_overnight["review"]["run_id"].startswith("auto-")
    story = audit_story(delegated_overnight["review"]["run_id"])
    assert any(e["tool"] == "git.merge_pr" for e in story), "no merge happened"
    assert not any(
        e["source"] == "process-gate" and e["rule"] == "gate_approved" for e in story
    ), "the merge was released by a human, not policy"
