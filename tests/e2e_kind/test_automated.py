"""The automated capstone: the company delivers its own work — merge and
all — with one human act, and that act is the contract's.

Everything is the heartbeat's: it scopes the slice the strategy names into
an ordered pair of tickets — the contract first, the implementation second
— implements each (a PR appears), reviews each delivered PR, and merges.
The contract PR edits the contract tree, so no automation may merge it: it
parks for a person, whose verdict is the arc's one human act. The
implementation then merges with no person at all — the change is in the
automated paths, the independent review passed, the head suite and the
contract tree as it stands on `main` are both green — under the compiled
`merge.resolve` decision. The autonomy runs through the merge.

What the platform still refuses is proven where it is exact and cheap, not
by paying for more unattended cluster runs here:

- a carve-out PR parks on the human `merge` signal even with a perfect
  review — `test_split_oracle_redteam` drives that on the cluster (the
  carve-out scenario), and `test_review_workflow` at the workflow layer;
- a failed quality layer feeds the bounded rework loop and, at the bound,
  escalates to the human — `test_review_workflow` (exhausted chain) and
  `test_review_activities` (depth-from-lineage) prove it exactly.

One refusal is proven here on the cluster, because it is positional: a
reviewer holds a shell in its sandbox, so the platform verifies the head
before any reviewer holds it. A red pull request carrying the tampering
reviewer's marker files rework with no reviewer dispatched at all.

The conftest orders this module after the other capstones, so its runs
are added last. Applies `examples/hello-saas-automated.yaml` (automated
merge) for the module and restores the pristine blueprint after.
"""

import re
import secrets
from collections import Counter

import pytest
import yaml

from common.audit import LIFECYCLE_FULFILL, REVIEW_VERDICT
from common.policy import GATE_RESOLVE, HEARTBEAT_WAKE, MERGE_RESOLVE
from common.tasks import review_run_task
from mock_brain.brain import (
    CONTRACT_FIRST_MARKER,
    CONTRACT_TEST_PATH,
    TAMPERING_REVIEW_MARKER,
)

from .blueprints import may_start, restore_pristine
from .cluster import (
    AUTOMATED,
    audit_story,
    await_run,
    console,
    db_now,
    human_acts_since,
    participants,
    per_run_cap,
    run_spend,
)
from .conftest import (
    apply_and_settle,
    assert_merge_closed_tickets,
    close_issue,
    close_pr,
    delete_repo_file,
    gate_merge_rules,
    github_api,
    open_slice_strategy,
    open_world_pr,
    pr_number,
    restore_strategy,
    run_review,
    run_ticket,
    scope_via_heartbeat,
    target_slug,
)


def _scope_tickets(env: dict, strategy: str) -> tuple[dict, dict[int, str]]:
    """Write the strategy, let the loop scope it, and return the scope run
    with the tickets it filed, titled — the company's own backlog."""
    scope = scope_via_heartbeat(env, strategy)
    assert scope["status"] == "done", f"unattended scope: {scope}"
    numbers = [
        int(m.group(1))
        for u in scope.get("ticket_urls") or []
        if (m := re.search(r"/issues/(\d+)$", u))
    ]
    assert numbers, f"the scope run filed no issue: {scope}"
    titles = {
        n: github_api(env, "GET", f"/repos/{target_slug(env)}/issues/{n}")
        .json()
        .get("title")
        or ""
        for n in numbers
    }
    return scope, titles


def _implement(number: int) -> dict:
    implement = run_ticket(number)
    assert implement["status"] == "done", f"unattended implement: {implement}"
    assert implement["pr_url"], "the unattended implement produced no PR"
    return implement


def _pr_files(env: dict, number: int) -> list[str]:
    return [
        f["filename"]
        for f in github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{number}/files"
        ).json()
    ]


# The heartbeat sequences the arc: the PM files contract-then-implement in
# one scope run, the loop implements the lower-numbered contract ticket
# first, its review parks for the human, and `max_concurrent_runs: 1` holds
# the implementation until that review is over: a park whose pull request
# edits the contract tree keeps its place, where any other park gives it
# back.


@pytest.fixture(scope="module")
def contract_first(stack: None, baseline_env: dict) -> dict:
    """One verdict out, nothing else. Reset `main` to the pre-contract
    state, let the strategy name a contract-first slice, and let the
    platform run the whole arc: the PM files the ordered pair, the
    contract crew authors the spec, its PR parks for the single human
    verdict, and then the implementation merges on a green base-ref
    contract with no person at all."""
    env = baseline_env
    apply_and_settle(AUTOMATED, env)
    # The declared deviation from the session baseline: the tree is declared
    # by the blueprint but deleted from `main`, so an implementation cannot
    # merge until the spec lands — which is the whole point of sequencing
    # the contract first. The arc itself puts the tree back (the one human
    # verdict), so the module exits at baseline.
    delete_repo_file(env, CONTRACT_TEST_PATH, "capstone: reset to pre-contract state")
    nonce = secrets.token_hex(3)
    result: dict = {"env": env, "since": db_now()}
    numbers: list[int] = []
    try:
        scope, titles = _scope_tickets(
            env, open_slice_strategy(nonce, CONTRACT_FIRST_MARKER)
        )
        result["scope"] = scope
        result["ticket_titles"] = titles
        numbers = list(titles)
        contract_issue = next(n for n, t in titles.items() if t.startswith("Contract:"))
        implement_issue = next(n for n, t in titles.items() if t.startswith("Implement:"))
        result["implement_issue"] = implement_issue

        # --- the contract run: author the spec, one human merges it --------
        contract_impl = _implement(contract_issue)
        result["contract_implement"] = contract_impl
        contract_pr = pr_number(contract_impl["pr_url"])
        assert contract_pr is not None, f"the contract run opened no PR: {contract_impl}"
        result["contract_pr"] = contract_pr
        c_review_run, c_review = run_review(contract_pr, approve=True)
        result["contract_review_run_id"] = c_review_run
        result["contract_review"] = c_review

        # --- the implement half: no human act at all ----------------------
        impl = _implement(implement_issue)
        result["implement"] = impl
        impl_pr = pr_number(impl["pr_url"])
        assert impl_pr is not None, f"the implement run opened no PR: {impl}"
        result["implement_pr"] = impl_pr
        i_review_run, i_review = run_review(impl_pr, approve=None)
        result["implement_review_run_id"] = i_review_run
        result["implement_review"] = i_review

        # The arc is over: every act a person made during it, counted now,
        # before the module's teardown applies anything.
        result["human_acts"] = human_acts_since(result["since"])
        result["contract_merged"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{contract_pr}"
        ).json()
        result["implement_merged"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{impl_pr}"
        ).json()
        result["implement_files"] = _pr_files(env, impl_pr)
        yield result
    finally:
        for issue in numbers:
            close_issue(env, issue)
        for pr in (result.get("contract_pr"), result.get("implement_pr")):
            if pr:
                close_pr(env, pr)
        restore_strategy(env)
        restore_pristine()


def test_every_run_of_the_arc_was_the_heartbeats(contract_first: dict):
    """No human started anything: the scope, both implements and both
    reviews ran under auto-minted ids."""
    runs = {
        "scope": contract_first["scope"]["run_id"],
        "contract implement": contract_first["contract_implement"]["run_id"],
        "contract review": contract_first["contract_review_run_id"],
        "implement": contract_first["implement"]["run_id"],
        "implement review": contract_first["implement_review_run_id"],
    }
    assert all(run_id.startswith("auto-") for run_id in runs.values()), runs


def test_the_pm_filed_contract_then_implement(contract_first: dict):
    """The unattended sequencing: one scope run filed exactly two
    tickets — the contract first, the implementation second — each naming its
    own crew. No human command sat between the two runs."""
    titles = contract_first["ticket_titles"]
    assert len(titles) == 2, titles
    assert sum(t.startswith("Contract:") for t in titles.values()) == 1
    assert sum(t.startswith("Implement:") for t in titles.values()) == 1
    contract, implement = sorted(titles)
    assert titles[contract].startswith("Contract:"), (
        "the contract ticket was not filed first"
    )


def test_the_contract_was_authored_by_the_contract_crew(contract_first: dict):
    """The spec came from different hands: the authoring run's ephemeral
    agents are the contract-authoring crew, whose role the delivery crew
    does not hold — positional independence, not a promise."""
    run_id = contract_first["contract_implement"]["run_id"]
    crews = {p.crew for p in participants(run_id)}
    assert crews == {"contract-authoring"}, crews


def test_the_contract_pr_needed_the_one_human_verdict(contract_first: dict):
    """Positional containment at merge: the contract PR is never an
    automated merge, so it parked and the person's verdict is what merged
    it — the spec changes under a person's hand, by construction."""
    story = audit_story(contract_first["contract_review_run_id"])
    rules = gate_merge_rules(story)
    assert "gate_automation_declined" in rules, rules
    assert "gate_approved" in rules, rules
    assert "gate_automated" not in rules, rules
    assert contract_first["contract_merged"]["merged"] is True, (
        "the contract never landed"
    )
    # The console says so without asking the git host: the run that
    # delivered the contract reads merged.
    delivery = console().run(contract_first["contract_implement"]["run_id"])
    assert delivery.pr_settled == "merged", delivery


def test_the_implementation_merged_on_a_green_contract_with_no_person(
    contract_first: dict,
):
    """The payoff: with the spec now on `main`, the implementation merged
    with no human command — the compiled decision released it, and the
    base-ref contract run against the head is what made it safe."""
    review = contract_first["implement_review"]
    story = audit_story(contract_first["implement_review_run_id"])
    assert review["status"] == "done", review
    assert contract_first["implement_merged"]["merged"] is True, (
        "automated work did not merge"
    )
    delivery = console().run(contract_first["implement"]["run_id"])
    assert delivery.pr_settled == "merged", delivery
    gate_rules = gate_merge_rules(story)
    assert "gate_automated" in gate_rules, gate_rules
    assert "gate_approved" not in gate_rules, "a person's verdict sneaked in"
    resolves = [e for e in story if e["tool"] == MERGE_RESOLVE]
    assert resolves and resolves[-1]["decision"] == "allow", resolves
    assert resolves[-1]["rule"] == "merge_resolve_automated", resolves[-1]
    assert any(e["tool"] == "git.merge_pr" and e["decision"] == "allow" for e in story), (
        "no merge tool call"
    )


def test_the_merged_work_fell_in_the_automated_paths(contract_first: dict):
    """The implementer put the slice where the bounds said a merge is
    automated: every file of the merged PR is under one of the blueprint's
    `automate.paths` — the paths the bounds named, not a guess."""
    gate = yaml.safe_load(AUTOMATED.read_text())["workflows"]["review"]["gates"]["merge"]
    automated = gate["automate"]["paths"]
    files = contract_first["implement_files"]
    assert files, "the merged PR changed no files"
    outside = [f for f in files if not any(f.startswith(p) for p in automated)]
    assert outside == [], f"merged files outside the automated paths: {outside}"


def test_the_automated_merge_closed_the_ticket(contract_first: dict):
    """A merge no person released still closes what it fulfilled: the
    implementation's ticket is closed by the platform after the merge."""
    assert_merge_closed_tickets(
        contract_first["env"],
        audit_story(contract_first["implement_review_run_id"]),
        contract_first["implement_pr"],
        [contract_first["implement_issue"]],
    )


def test_both_quality_layers_came_before_the_release(contract_first: dict):
    """The merge skipped no layer: the platform verified the head before
    any reviewer held it, the reviewer's verdict was journaled (a pass),
    the contract tree as it stands on `main` was run against the head
    after it, and only then did `merge.resolve` release the merge — one
    run_id, in that order."""
    story = audit_story(contract_first["implement_review_run_id"])
    verifies = [
        i
        for i, e in enumerate(story)
        if e["tool"] == "sandbox.run_tests"
        and e["lifecycle"] == "platform"
        and e["decision"] == "allow"
    ]
    verdicts = [i for i, e in enumerate(story) if e["tool"] == REVIEW_VERDICT]
    releases = [
        i
        for i, e in enumerate(story)
        if e["tool"] == MERGE_RESOLVE and e["decision"] == "allow"
    ]
    assert len(verifies) >= 2, "the head suite and the contract replay both run"
    assert verdicts and story[verdicts[-1]]["decision"] == "allow", verdicts
    assert releases, "the merge was never released"
    assert min(verifies) < verdicts[-1] < max(verifies) < releases[-1], (
        verifies,
        verdicts,
        releases,
    )


def test_the_arc_had_exactly_one_human_act(contract_first: dict):
    """The claim as an invariant, counted at the console: across every run
    of the arc a person did one thing — the contract's verdict. The rest
    of the console's record is the harness's wakes, standing in for the
    looks the loop would take on its own."""
    acts = Counter(contract_first["human_acts"])
    assert acts.pop(GATE_RESOLVE, 0) == 1, contract_first["human_acts"]
    assert set(acts) <= {HEARTBEAT_WAKE}, contract_first["human_acts"]


def test_the_contract_arc_stayed_within_budget(contract_first: dict):
    """Bounded money across every run of the arc: each was metered, and
    each stayed under the applied blueprint's per-run cap."""
    cap = per_run_cap()
    for key in ("scope", "contract_implement", "implement"):
        spend = run_spend(contract_first[key]["run_id"])
        assert 0 < spend < cap, f"{key} overspent: {spend}"


# -- a reviewer that tampers with its sandbox moves no check ----------------

# A pull request whose own suite is red, carrying the marker that makes the
# mock reviewer rewrite its sandbox (a conftest.py that collects nothing)
# and pass. Under the automated gate the platform verifies the head first.
RED_TEST_PY = (
    f"# {TAMPERING_REVIEW_MARKER}: the reviewer is told to make this suite"
    " collect nothing.\n"
    "def test_this_pull_request_is_red():\n"
    '    assert False, "this pull request\'s suite is red"\n'
)


@pytest.fixture(scope="module")
def tampered_review(stack: None, baseline_env: dict) -> dict:
    """A red pull request the world opened, reviewed by a person's start
    under the automated gate. The loop may only scope here, so it neither
    reviews the PR on its own nor implements the rework ticket the
    platform files; the module closes both."""
    env = baseline_env
    apply_and_settle(may_start(AUTOMATED, "scope"), env)
    nonce = secrets.token_hex(3)
    result: dict = {"env": env}
    try:
        number = open_world_pr(
            env,
            f"feature/tamper-{nonce}",
            {f"tests/unit/test_tamper_{nonce}.py": RED_TEST_PY},
            f"Tighten the unit suite ({nonce})",
        )
        result["pr_number"] = number
        result["run_id"] = console().start_run("review", number).run_id
        result["review"] = await_run(
            review_run_task(number), terminal=True, workflow="review"
        )
        result["story"] = audit_story(result["run_id"])
        result["pr"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{number}"
        ).json()
        ticket = re.search(r"/issues/(\d+)", result["review"].get("error") or "")
        result["rework_issue"] = int(ticket.group(1)) if ticket else None
        yield result
    finally:
        close_issue(env, result.get("rework_issue"))
        close_pr(env, result.get("pr_number") or 0)
        restore_pristine()


def test_a_red_head_files_rework_before_any_reviewer(tampered_review: dict):
    """The platform's verify of record ran on the sandbox as provisioned,
    went red and filed the rework: no crew was fulfilled, so no reviewer
    ever held a shell in a sandbox a platform check would read."""
    record = tampered_review["review"]
    assert record["status"] == "failed", record
    assert "platform verify red" in record["error"], record["error"]
    assert tampered_review["rework_issue"], record["error"]
    story = tampered_review["story"]
    verifies = [
        e
        for e in story
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert verifies, "no platform verify in the story"
    assert not any(e["tool"] == LIFECYCLE_FULFILL for e in story), (
        "a reviewer was fulfilled for a head the platform had refused"
    )
    assert not any(e["tool"] == REVIEW_VERDICT for e in story)


def test_the_red_pr_merged_nothing_and_was_superseded(tampered_review: dict):
    pr = tampered_review["pr"]
    assert pr["merged"] is False
    assert pr["state"] == "closed"  # the rework ticket supersedes it
    assert not any(e["tool"] == "git.merge_pr" for e in tampered_review["story"])
