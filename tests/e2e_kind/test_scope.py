"""Scoping on the live cluster: the company originates its own work.

No human hands the PM a text. The standing heartbeat reads the company's
documents at `base_ref`, asks the conductor what to do, and — when the
strategy names an open slice — starts a `scope` run: the PM files the
slice's ticket with its `Crew:` trailer and its summary comment, the run
row links the ticket, and the PM's tool calls appear in the audit story
under the run's `run_id`, as the standing `pm-1`. The same loop, on a
strategy that names nothing, idles: a delivered strategy is a valid state,
not a failure. And the conductor cannot flood its own backlog: at the
blueprint's `max_open_tickets` the platform refuses `scope` by name, with
no model retry.

Applies a `scope`-only profile so the ticket the PM files waits for the
module rather than being implemented on the next wake; restores the
pristine profile and the resting strategy after. The denial half ("a PM
attempt at sandbox.exec or git.open_pr is denied at the gateway and
audited") is proven by
test_governance.py::test_pm_is_denied_implementer_tools_at_the_gateway.
"""

import re
import secrets

import pytest

from common.tasks import SCOPE_TASK
from mockllm.brain import NEXT_SLICE_MARKER, PM_SUMMARY

from .blueprints import HELLO_SAAS, derived_blueprint, may_start, restore_pristine
from .cluster import audit_story, await_run, run_by_task
from .conftest import (
    apply_profile,
    await_new_ticks,
    bare_heartbeat_id,
    close_issue,
    github_api,
    heartbeat_ticks,
    open_slice_strategy,
    restore_strategy,
    target_slug,
    wake_bare_heartbeat,
    wake_until_run_started,
    write_strategy,
)


def _scope_only(max_open_tickets: int):
    """The pristine example with scoping as the loop's only initiative and
    the given backlog cap."""

    def cap(document: dict) -> None:
        document["heartbeat"]["max_open_tickets"] = max_open_tickets

    return derived_blueprint(
        may_start(HELLO_SAAS, "scope"), cap, f"cap-{max_open_tickets}"
    )


@pytest.fixture(scope="module")
def scoped(stack: None, repo_baseline: dict) -> dict:
    """Write a strategy naming one slice, wake the loop, and let it scope:
    hand back the run, the ticket and the ticks for the assertions. The
    strategy and the profile go back at the end."""
    env = repo_baseline
    apply_profile(_scope_only(5), env)
    nonce = secrets.token_hex(3)
    result: dict = {"env": env, "heartbeat_id": bare_heartbeat_id(), "nonce": nonce}
    try:
        write_strategy(env, open_slice_strategy(nonce, NEXT_SLICE_MARKER))
        wake_until_run_started(SCOPE_TASK, "scope")
        result["record"] = await_run(SCOPE_TASK, terminal=True, deadline_s=600)
        urls = result["record"].get("ticket_urls") or []
        match = next((re.search(r"/issues/(\d+)$", u) for u in urls if u), None)
        result["issue_number"] = int(match.group(1)) if match else None
        yield result
    finally:
        close_issue(env, result.get("issue_number"))
        restore_strategy(env)
        restore_pristine()


def test_the_loop_started_the_scope_run_itself(scoped: dict):
    """No human started anything: the run is the heartbeat's — an
    auto-minted id, an authorized `heartbeat.start` under it, and this
    heartbeat's own started tick naming it."""
    record = scoped["record"]
    assert record["run_id"].startswith("auto-"), record["run_id"]
    assert record["workflow"] == "scope"
    assert record["status"] == "done", f"scope failed: {record}"
    assert any(
        e["tool"] == "heartbeat.start" and e["decision"] == "allow"
        for e in audit_story(record["run_id"])
    )
    assert any(
        t["decision"] == "allow" and f"scope_as_{record['run_id']}" in (t["rule"] or "")
        for t in heartbeat_ticks(scoped["heartbeat_id"])
    ), "this heartbeat never recorded starting the scope run"


def test_the_strategy_became_a_ticket_with_the_pms_summary(scoped: dict):
    """The affirmative half: the run links the ticket it filed, the ticket
    is real, carries the crew trailer and the PM's summary comment, and the
    PM's tool calls are the run's story — all as the standing pm-1."""
    env, record = scoped["env"], scoped["record"]
    assert record["ticket_urls"], "the runs row links no tickets"
    number = scoped["issue_number"]
    assert number is not None, record["ticket_urls"]
    issue = github_api(env, "GET", f"/repos/{target_slug(env)}/issues/{number}").json()
    assert issue["state"] == "open"
    assert issue["body"].rstrip().endswith("Crew: feature-delivery"), issue["body"]
    assert scoped["nonce"] in issue["body"], (
        "the strategy's words did not reach the ticket"
    )
    comments = github_api(
        env, "GET", f"/repos/{target_slug(env)}/issues/{number}/comments"
    ).json()
    assert [c["body"] for c in comments] == [PM_SUMMARY], comments

    story = [e for e in audit_story(record["run_id"]) if e["source"] == "gateway"]
    assert {"issues.create", "issues.comment"} <= {e["tool"] for e in story}
    for event in story:
        assert event["agent_id"] == "pm-1"
        assert event["lifecycle"] == "standing"
        assert event["company"] == "hello-saas"
        assert event["decision"] == "allow"
        assert event["workflow"] == "scope"


def test_the_backlog_cap_refuses_a_second_scope_by_name(scoped: dict):
    """The conductor may not flood its own backlog: with the cap at one
    open ticket and one open, the PM's next `scope` is refused by the
    platform — a `heartbeat.decide` deny naming the cap, a refused tick,
    and no second scope run."""
    env = scoped["env"]
    apply_profile(_scope_only(1), env)
    mark = len(heartbeat_ticks(scoped["heartbeat_id"]))
    wake_bare_heartbeat()
    (tick,) = await_new_ticks(mark, 1)
    assert tick["decision"] == "deny" and "max_open_tickets" in (tick["rule"] or ""), tick
    refusals = [
        e
        for e in audit_story(scoped["heartbeat_id"])
        if e["tool"] == "heartbeat.decide" and e["decision"] == "deny"
    ]
    assert any("max_open_tickets" in (e["rule"] or "") for e in refusals), refusals
    runs = [r for r in [run_by_task(SCOPE_TASK, workflow="scope")] if r is not None]
    assert all(r["run_id"] == scoped["record"]["run_id"] for r in runs)


def test_a_strategy_naming_no_slice_leaves_the_loop_idle(scoped: dict):
    """A delivered strategy is a valid state: with the resting strategy
    back and the backlog cleared, the woken loop asks the PM and the PM
    chooses idle — no scope run, no ticket."""
    env = scoped["env"]
    close_issue(env, scoped["issue_number"])
    restore_strategy(env)
    apply_profile(_scope_only(5), env)
    mark = len(heartbeat_ticks(scoped["heartbeat_id"]))
    wake_bare_heartbeat()
    (tick,) = await_new_ticks(mark, 1)
    assert tick["rule"] == "tick_idle:pm_chose_idle", tick
