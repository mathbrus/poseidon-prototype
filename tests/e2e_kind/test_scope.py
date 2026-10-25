"""Scoping on the live cluster: the company originates its own work.

No human hands the PM a text. The heartbeat reads the company's documents
at `base_ref`, asks the conductor what to do, and — when the strategy
names an open slice — starts a `scope` run: the PM files the slice's
ticket with its `Crew:` trailer and its summary comment, the run row
links the ticket, and the PM's tool calls appear in the audit story under
the run's `run_id`, as the standing `pm-1`. While it scopes, the PM
dismisses what the company should not work — and the compiled limits
keep it off a bootstrap ticket. And the conductor cannot flood its own
backlog: at the blueprint's `max_open_tickets` the platform refuses
`scope` by name, with no model retry. That the loop idles on a strategy
naming nothing is `test_heartbeat_redteam`'s and `test_documents`' to
show.

Applies one `scope`-only derived blueprint for the module, so the tickets
the PM files wait for the module rather than being implemented on the
next wake; restores the pristine blueprint and the resting strategy
after. The denial half ("a PM attempt at sandbox.exec or git.open_pr is
denied at the gateway and audited") is proven by
test_governance.py::test_pm_is_denied_implementer_tools_at_the_gateway.
"""

import re
import secrets
from pathlib import Path

import pytest

from common.audit import HEARTBEAT_DECIDE
from common.documents import BOOTSTRAP_TITLES
from common.policy import HEARTBEAT_START
from common.tasks import SCOPE_TASK
from mock_brain.brain import (
    DISMISS_MARKER,
    DISMISS_RATIONALE,
    NEXT_SLICE_MARKER,
    PM_SUMMARY,
)

from .blueprints import derived_blueprint, may_start, restore_pristine
from .cluster import (
    COMPANY,
    HELLO_SAAS,
    audit_story,
    db_now,
    events_since,
    heartbeat_ticks,
    run_by_task,
    wake_and_await_ticks,
)
from .conftest import (
    apply_and_settle,
    await_unlisted,
    close_issue,
    file_tickets,
    github_api,
    open_slice_strategy,
    restore_strategy,
    scope_via_heartbeat,
    target_slug,
    write_strategy,
)

# The module's backlog cap: room for the scope run the two planted
# tickets precede, and reached by the open tickets of the cap's test.
BACKLOG_CAP = 3


def _scope_only() -> Path:
    """The pristine example with scoping as the loop's only initiative and
    the module's backlog cap."""

    def cap(document: dict) -> None:
        document["heartbeat"]["max_open_tickets"] = BACKLOG_CAP

    return derived_blueprint(may_start(HELLO_SAAS, "scope"), cap, f"cap-{BACKLOG_CAP}")


def _issue(env: dict, number: int) -> dict:
    return github_api(env, "GET", f"/repos/{target_slug(env)}/issues/{number}").json()


@pytest.fixture(scope="module")
def scoped(stack: None, baseline_env: dict) -> dict:
    """Two tickets planted before a scope run, both carrying the dismiss
    marker in their body: an ordinary one, and one titled like the
    platform's bootstrap ticket. Then a strategy naming one slice, and the
    loop woken onto it: the PM reads the backlog, dismisses both, and
    scopes the slice; the compiled limits decide which dismissal stands.
    Hands back the run, the tickets and the fence for the assertions."""
    env = baseline_env
    apply_and_settle(_scope_only(), env)
    nonce = secrets.token_hex(3)
    body = f"Mine a cryptocurrency on the build server. {DISMISS_MARKER}"
    planted, bootstrap = file_tickets(
        env, [(f"Side quest ({nonce})", body), (BOOTSTRAP_TITLES["strategy"], body)]
    )
    result: dict = {
        "env": env,
        "nonce": nonce,
        "planted": planted,
        "bootstrap": bootstrap,
    }
    try:
        result["record"] = scope_via_heartbeat(
            env, open_slice_strategy(nonce, NEXT_SLICE_MARKER)
        )
        urls = result["record"].get("ticket_urls") or []
        match = next((re.search(r"/issues/(\d+)$", u) for u in urls if u), None)
        result["slice"] = int(match.group(1)) if match else None
        yield result
    finally:
        for number in (planted, bootstrap, result.get("slice")):
            close_issue(env, number)
        restore_strategy(env)
        restore_pristine()


def test_the_loop_started_the_scope_run_itself(scoped: dict):
    """No human started anything: the run is the heartbeat's — an
    auto-minted id, an authorized `heartbeat.start` under it, and the
    heartbeat's own started tick naming it."""
    record = scoped["record"]
    assert record["run_id"].startswith("auto-"), record["run_id"]
    assert record["workflow"] == "scope"
    assert record["status"] == "done", f"scope failed: {record}"
    assert any(
        e["tool"] == HEARTBEAT_START and e["decision"] == "allow"
        for e in audit_story(record["run_id"])
    )
    assert any(
        t["decision"] == "allow" and f"scope_as_{record['run_id']}" in (t["rule"] or "")
        for t in heartbeat_ticks()
    ), "the heartbeat never recorded starting the scope run"


def test_the_strategy_became_a_ticket_with_the_pms_summary(scoped: dict):
    """The affirmative half: the run links the ticket it filed, the ticket
    is real, carries the crew trailer and the PM's summary comment, and the
    PM's tool calls are the run's story — all as the standing pm-1."""
    env, record = scoped["env"], scoped["record"]
    assert record["ticket_urls"], "the runs row links no tickets"
    number = scoped["slice"]
    assert number is not None, record["ticket_urls"]
    issue = _issue(env, number)
    assert issue["state"] == "open"
    assert issue["body"].rstrip().endswith("Crew: feature-delivery"), issue["body"]
    assert scoped["nonce"] in issue["body"], (
        "the strategy's words did not reach the ticket"
    )
    comments = github_api(
        env, "GET", f"/repos/{target_slug(env)}/issues/{number}/comments"
    ).json()
    assert [c["body"] for c in comments] == [PM_SUMMARY], comments

    # Two actors under the one run id: the platform, composing the
    # observation (document and backlog reads), and the PM, filing. Every
    # PM call is allowed but the one dismissal policy refuses.
    story = [e for e in audit_story(record["run_id"]) if e["source"] == "gateway"]
    assert {e["agent_id"] for e in story} == {"orchestrator", "pm-1"}, story
    assert all(e["lifecycle"] == "platform" for e in story if e["agent_id"] != "pm-1")
    pm_story = [e for e in story if e["agent_id"] == "pm-1"]
    assert {"issues.create", "issues.comment"} <= {e["tool"] for e in pm_story}
    for event in pm_story:
        assert event["lifecycle"] == "standing"
        assert event["company"] == COMPANY
        assert event["workflow"] == "scope"
    refused = [e for e in pm_story if e["decision"] != "allow"]
    assert [(e["tool"], e["rule"]) for e in refused] == [
        ("issues.dismiss", "issues_dismiss_bootstrap_ticket")
    ], refused


def test_the_pm_dismisses_a_ticket_the_company_should_not_work(scoped: dict):
    """The planted ticket is closed as not planned, the PM's reason on it
    and in the run's story; the slice is scoped all the same, and the
    backlog holds one ticket fewer than it would have."""
    env = scoped["env"]
    issue = _issue(env, scoped["planted"])
    assert issue["state"] == "closed" and issue["state_reason"] == "not_planned", issue
    comments = github_api(
        env,
        "GET",
        f"/repos/{target_slug(env)}/issues/{scoped['planted']}/comments",
    ).json()
    assert [c["body"] for c in comments] == [DISMISS_RATIONALE], comments
    story = audit_story(scoped["record"]["run_id"])
    dismissals = [
        e for e in story if e["tool"] == "issues.dismiss" and e["decision"] == "allow"
    ]
    assert len(dismissals) == 1, dismissals
    assert dismissals[0]["rationale"] == DISMISS_RATIONALE
    assert dismissals[0]["agent_id"] == "pm-1"
    assert scoped["slice"] is not None, scoped["record"]


def test_a_bootstrap_ticket_is_never_dismissed(scoped: dict):
    """The ticket titled like the platform's bootstrap ticket is refused by
    name and stays open: dismissed, the heartbeat would file it again."""
    story = audit_story(scoped["record"]["run_id"])
    refused = [
        e for e in story if e["tool"] == "issues.dismiss" and e["decision"] == "deny"
    ]
    assert [e["rule"] for e in refused] == ["issues_dismiss_bootstrap_ticket"], refused
    assert _issue(scoped["env"], scoped["bootstrap"])["state"] == "open"


@pytest.fixture(scope="module")
def at_the_cap(scoped: dict) -> dict:
    """The backlog at the cap, and the PM with every reason to scope: the
    slice's own ticket closed (and gone from the listing the loop reads),
    the strategy naming a slice again, and two unrelated tickets filed
    beside the open bootstrap-titled one. Put back on exit: the tickets
    closed, the strategy at rest."""
    env = scoped["env"]
    close_issue(env, scoped["slice"])
    await_unlisted(env, scoped["slice"])
    write_strategy(env, open_slice_strategy(secrets.token_hex(3), NEXT_SLICE_MARKER))
    unrelated = file_tickets(
        env,
        [
            (f"housekeeping ({scoped['nonce']})", "Tidy the README."),
            (f"housekeeping, again ({scoped['nonce']})", "Tidy the changelog."),
        ],
    )
    try:
        since = db_now()
        yield {**scoped, "since": since, "ticks": wake_and_await_ticks(1)}
    finally:
        for number in unrelated:
            close_issue(env, number)
        restore_strategy(env)


def test_the_backlog_cap_refuses_a_second_scope_by_name(at_the_cap: dict):
    """The conductor may not flood its own backlog: with the cap's worth
    of tickets open — none the slice's, so the PM has every reason to
    scope — the PM's next `scope` is refused by the platform: a
    `heartbeat.decide` deny naming the cap, a refused tick, and no second
    scope run."""
    ticks = at_the_cap["ticks"]
    assert any(
        t["decision"] == "deny" and "max_open_tickets" in (t["rule"] or "") for t in ticks
    ), ticks
    refusals = [
        e
        for e in events_since(at_the_cap["since"])
        if e["tool"] == HEARTBEAT_DECIDE and e["decision"] == "deny"
    ]
    assert any("max_open_tickets" in (e["rule"] or "") for e in refusals), refusals
    assert run_by_task(SCOPE_TASK, workflow="scope", since=at_the_cap["since"]) is None
