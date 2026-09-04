"""The delegated capstone: the company delivers its own work — merge and
all — with zero human acts.

The overnight capstone (`test_overnight`) proved the company runs its own
slice to the human merge gate: one human act, the approval out. A
policy-resolved merge gate removes that act for a declared class of work,
so the whole arc has *no* human act at all.

Everything is the standing heartbeat's: it scopes the slice the strategy
names, implements the ticket (a PR appears), reviews the delivered PR, and
— because the change is in the delegated class, the independent review
passed, and the platform's own checks are green — merges it. No approval,
no signal, no command. The autonomy runs through the merge.

What the platform still refuses is proven where it is exact and cheap, not
by paying for more unattended cluster runs here:

- a carve-out PR parks on the human `merge` signal even with a perfect
  review — `test_split_oracle_redteam` drives that on the cluster (the
  carve-out scenario), and `test_review_workflow` at the workflow layer;
- a failed quality layer feeds the bounded rework loop and, at the bound,
  escalates to the human — `test_review_workflow` (exhausted chain) and
  `test_review_activities` (depth-from-lineage) prove it exactly.

So this capstone pins the one thing only a full unattended cluster run can
show: passing in-class work reaches `main` with no human anywhere.

The conftest orders this module after the other capstones, so its runs
are added last. Applies `examples/hello-saas-delegated.yaml` (delegated
merge) for the module and restores the pristine profile after.
"""

import re
import secrets

import pytest

from common.tasks import issue_run_task, review_run_task
from mockllm.brain import CONTRACT_FIRST_MARKER, CONTRACT_TEST_PATH, NEXT_SLICE_MARKER

from .blueprints import DELEGATED, restore_pristine
from .cluster import audit_story, await_run, participants, run_spend
from .conftest import (
    apply_profile,
    bare_heartbeat_id,
    close_issue,
    close_pr,
    delete_repo_file,
    gate_merge_rules,
    github_api,
    open_slice_strategy,
    pr_number,
    restore_strategy,
    run_review,
    scope_via_heartbeat,
    target_slug,
    wake_until_run_started,
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
    wake_until_run_started(issue_run_task(number), "implement")
    implement = await_run(issue_run_task(number), terminal=True)
    assert implement["status"] == "done", f"unattended implement: {implement}"
    assert implement["pr_url"], "the unattended implement produced no PR"
    return implement


@pytest.fixture(scope="module")
def delegated_overnight(stack: None, repo_baseline: dict) -> dict:
    """Nothing in, nothing out: let the company scope, implement, review,
    and MERGE its own in-class PR with no command. Hand back every
    artifact for the assertions.

    Depends on the session `repo_baseline`: the delegated blueprint declares
    `contract_tests`, so the unattended merge needs the contract tree on the
    base ref — without it the composed verify is red and the arc reworks
    instead of merging."""
    env = repo_baseline
    apply_profile(DELEGATED, env)
    nonce = secrets.token_hex(3)
    result: dict = {"env": env, "heartbeat_id": bare_heartbeat_id()}
    try:
        scope, titles = _scope_tickets(env, open_slice_strategy(nonce, NEXT_SLICE_MARKER))
        result["scope"] = scope
        (issue_number,) = titles
        result["issue_number"] = issue_number
        implement = _implement(issue_number)
        result["implement"] = implement
        number = pr_number(implement["pr_url"])
        assert number is not None, f"not a PR url: {implement['pr_url']!r}"
        result["pr_number"] = number
        # The implement's finishing nudge wakes the heartbeat onto the
        # delivered PR; it reviews it and — in-class, review passed,
        # checks green — MERGES it. No human signal is sent.
        wake_until_run_started(review_run_task(number), "review")
        result["review"] = await_run(review_run_task(number), terminal=True)
        result["merged_pr"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{number}"
        ).json()
        yield result
    finally:
        close_pr(env, result.get("pr_number") or 0)
        close_issue(env, result.get("issue_number"))
        restore_strategy(env)
        restore_pristine()


def test_the_company_scoped_implemented_and_reviewed_unattended(
    delegated_overnight: dict,
):
    """All three runs are the heartbeat's — auto-minted run ids, no human
    command started any of them."""
    for key in ("scope", "implement", "review"):
        assert delegated_overnight[key]["run_id"].startswith("auto-"), key
    assert delegated_overnight["review"]["workflow"] == "review"


def test_the_in_class_pr_merged_with_no_human_act(delegated_overnight: dict):
    """The headline: the PR is merged on GitHub, and no human ever touched
    anything. The review run finished done and the PR is merged."""
    assert delegated_overnight["review"]["status"] == "done", delegated_overnight[
        "review"
    ]
    assert delegated_overnight["merged_pr"]["merged"] is True, (
        "the in-class PR did not merge unattended"
    )


def test_the_merge_was_released_by_policy_not_a_human(delegated_overnight: dict):
    """The merge was released by the compiled decision, not a human gate.
    The review's story carries a policy_resolved gate outcome and a
    merge.resolve allow — and NO human gate_approved event."""
    story = audit_story(delegated_overnight["review"]["run_id"])
    gate_rules = gate_merge_rules(story)
    assert "gate_policy_resolved" in gate_rules, gate_rules
    assert "gate_approved" not in gate_rules, "a human approval sneaked in"
    resolves = [e for e in story if e["tool"] == "merge.resolve"]
    assert resolves and resolves[-1]["decision"] == "allow", resolves
    assert resolves[-1]["rule"] == "merge_resolve_delegated"
    assert any(e["tool"] == "git.merge_pr" for e in story), "no merge tool call"


def test_both_quality_layers_were_recorded(delegated_overnight: dict):
    """The merge did not skip the layers: the reviewer's verdict was
    journaled (allow) and the platform's own decision saw the real facts —
    the run_id ties findings, verdict, checks and merge into one story."""
    story = audit_story(delegated_overnight["review"]["run_id"])
    verdicts = [e for e in story if e["tool"] == "review.verdict"]
    assert verdicts and verdicts[-1]["decision"] == "allow", verdicts
    assert any(e["tool"] == "heartbeat.start" and e["decision"] == "allow" for e in story)


def test_delegated_arc_had_no_human_act(delegated_overnight: dict):
    """The claim, as an invariant: every run ran under an auto- id, the
    merge happened, and it was released by policy — never by a human
    gate signal."""
    story = audit_story(delegated_overnight["review"]["run_id"])
    assert any(e["tool"] == "git.merge_pr" for e in story), "no merge happened"
    assert not any(
        e["source"] == "process-gate" and e["rule"] == "gate_approved" for e in story
    ), "the merge was released by a human, not policy"


# --- the contract-first arc: the split oracle's capstone --------------------
#
# The delegated arc above proves in-class work merges unattended. This proves
# the shape the split oracle adds: the company writes the *spec* first, under
# different hands and different authority, and only then implements against
# it. The whole arc has exactly one human act — the merge of the contract PR
# — and everything else is the platform's. The heartbeat sequences it: the
# PM files contract-then-implement in one scope run, the loop implements
# the lower-numbered contract ticket first, its review parks for the human,
# and `max_concurrent_runs: 1` holds the implementation until that review
# is over.


@pytest.fixture(scope="module")
def contract_first(stack: None, repo_baseline: dict) -> dict:
    """One contract merge out, nothing else. Reset `main` to the
    pre-contract state, let the strategy name a contract-first slice, and
    let the platform run the whole arc: the PM files the ordered pair, the
    contract crew authors the spec, its PR parks for the single human
    merge, and then the implementation auto-merges on a green base-ref
    contract — no other command."""
    env = repo_baseline
    apply_profile(DELEGATED, env)
    # The declared deviation from the session baseline: the tree is declared
    # by the blueprint but deleted from `main`, so an implementation cannot
    # merge until the spec lands — which is the whole point of sequencing
    # the contract first. The arc itself puts the tree back (the one human
    # merge), so the module exits at baseline.
    delete_repo_file(env, CONTRACT_TEST_PATH, "capstone: reset to pre-contract state")
    nonce = secrets.token_hex(3)
    result: dict = {"env": env}
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

        # --- the contract run: author the spec, one human merges it --------
        contract_impl = _implement(contract_issue)
        result["contract_implement"] = contract_impl
        contract_pr = pr_number(contract_impl["pr_url"])
        assert contract_pr is not None, f"the contract run opened no PR: {contract_impl}"
        result["contract_pr"] = contract_pr
        c_review_run, c_review = run_review(env, contract_pr, approve=True)
        result["contract_review_run_id"] = c_review_run
        result["contract_review"] = c_review

        # --- the implement phase: no human act at all ----------------------
        impl = _implement(implement_issue)
        result["implement"] = impl
        impl_pr = pr_number(impl["pr_url"])
        assert impl_pr is not None, f"the implement run opened no PR: {impl}"
        result["implement_pr"] = impl_pr
        i_review_run, i_review = run_review(env, impl_pr, approve=None)
        result["implement_review_run_id"] = i_review_run
        result["implement_review"] = i_review

        result["contract_merged"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{contract_pr}"
        ).json()
        result["implement_merged"] = github_api(
            env, "GET", f"/repos/{target_slug(env)}/pulls/{impl_pr}"
        ).json()
        yield result
    finally:
        for pr in (result.get("contract_pr"), result.get("implement_pr")):
            if pr:
                close_pr(env, pr)
        for issue in numbers:
            close_issue(env, issue)
        restore_strategy(env)
        restore_pristine()


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
    """The spec came from different hands: the authoring run's participants
    are the contract-authoring crew, a template the delivery crew does not
    contain — positional independence, not a promise."""
    run_id = contract_first["contract_implement"]["run_id"]
    crews = {p["crew_template"] for p in participants(run_id)}
    assert crews == {"contract-authoring"}, crews


def test_the_contract_pr_needed_the_one_human_merge(contract_first: dict):
    """Positional containment at merge: the contract PR could not
    policy-merge, so it parked and the human's approval is what merged it —
    the spec changes under a person's hand, by construction."""
    story = audit_story(contract_first["contract_review_run_id"])
    rules = gate_merge_rules(story)
    assert "gate_policy_declined" in rules, rules
    assert "gate_approved" in rules, rules
    assert "gate_policy_resolved" not in rules, rules
    assert contract_first["contract_merged"]["merged"] is True, (
        "the contract never landed"
    )


def test_the_implementation_auto_merged_on_a_green_contract(contract_first: dict):
    """The payoff: with the spec now on `main`, the implementation merged
    with no human command — the compiled decision released it, and the
    base-ref contract run against the head is what made it safe."""
    review = contract_first["implement_review"]
    story = audit_story(contract_first["implement_review_run_id"])
    assert review["status"] == "done", review
    assert contract_first["implement_merged"]["merged"] is True, (
        "in-class work did not merge"
    )
    gate_rules = gate_merge_rules(story)
    assert "gate_policy_resolved" in gate_rules, gate_rules
    assert "gate_approved" not in gate_rules, (
        "a human approval sneaked into the implement merge"
    )
    resolves = [e for e in story if e["tool"] == "merge.resolve"]
    assert resolves and resolves[-1]["decision"] == "allow", resolves
    assert resolves[-1]["rule"] == "merge_resolve_delegated", resolves[-1]


def test_contract_arc_had_exactly_one_human_act(contract_first: dict):
    """The claim as an invariant: across every run, the only human act was
    the contract merge. The implementation's merge was policy's, and
    nothing else asked for a person."""
    impl_story = audit_story(contract_first["implement_review_run_id"])
    assert not any(
        e["source"] == "process-gate" and e["rule"] == "gate_approved" for e in impl_story
    ), "the implementation was merged by a human, not policy"
    assert any(e["tool"] == "git.merge_pr" for e in impl_story), (
        "no unattended merge happened"
    )


def test_the_contract_arc_stayed_within_budget(contract_first: dict):
    """Bounded money across every run of the arc."""
    for key in ("scope", "contract_implement", "implement"):
        spend = run_spend(contract_first[key]["run_id"])
        assert 0 <= spend < 5.0, f"{key} overspent: {spend}"
