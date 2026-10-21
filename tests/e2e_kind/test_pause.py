"""A human holds the company: `poseidon pause` holds the standing
heartbeat and nothing else, proven live.

The pause is a console act like `wake` and `delete` — decided against
`company.humans`, audited under the person's name with the reason — and
a fact the loop reads on every tick, not a signal it trusts. With a
ticket on the backlog the loop would otherwise start, a paused tick
records `tick_paused:<name>`, asks the PM nothing and starts nothing;
`heartbeat show` says `paused`, `company show` says who holds it; a wake
(a person's, or a finished run's) ticks once more and leaves it paused;
a verified person the company did not name is refused by name. A resume
lifts the hold and the next tick starts the work the pause held. A run
in flight when the pause lands is not the loop's: it finishes, and its
end wakes a loop that stays paused.

The module arrives on the example with `may_start: [implement]` so the
delivered PR waits for the world instead of being reviewed on the next
wake, and the pristine profile is restored after. Every pause the module
takes it lifts; the session sweeper lifts one a failure path left.
"""

import secrets

import pytest

from common.policy import HEARTBEAT_PAUSE, HEARTBEAT_RESUME
from common.tasks import issue_run_task
from common.workflows import TICK_PAUSED, TICK_STARTED, parse_tick_rule, tick_rule
from poseidon_cli import PolicyDenied

from .blueprints import HELLO_SAAS, may_start, restore_pristine
from .cluster import (
    COMPANY,
    audit_story,
    await_bare_heartbeat_asleep,
    await_new_ticks,
    await_run,
    bare_heartbeat_id,
    console,
    heartbeat_ticks,
    run_by_task,
    wake_and_await_ticks,
)
from .conftest import (
    apply_profile,
    close_issue,
    close_pr_url,
    file_ticket,
    lift_pause,
)

HUMAN = "owner"


@pytest.fixture(scope="module")
def pause_profile(stack: None, baseline_env: dict):
    apply_profile(may_start(HELLO_SAAS, "implement"), baseline_env)
    yield
    restore_pristine()


@pytest.fixture(scope="module")
def held(pause_profile, env: dict) -> dict:
    """A startable backlog (one ticket), then the pause: the act itself
    wakes the loop, so the tick that follows is the module's own and is
    the one that reads the pause."""
    nonce = secrets.token_hex(3)
    issue_number = file_ticket(
        env,
        f"Add a slugify utility ({nonce})",
        "Add a slugify utility with tests.\n\n"
        f"Deliver your work on the branch `feature/slugify-{nonce}`.",
    )
    mark = len(heartbeat_ticks(bare_heartbeat_id()))
    paused = console(env).pause("reading the ledger with nothing moving")
    (tick,) = await_new_ticks(mark, 1)
    state = {
        "issue_number": issue_number,
        "task": issue_run_task(issue_number),
        "paused": paused,
        "tick": tick,
        "pr_url": None,
    }
    try:
        yield state
    finally:
        lift_pause(env)
        close_pr_url(env, state.get("pr_url"))
        close_issue(env, issue_number)


def test_a_paused_tick_starts_nothing_on_a_startable_backlog(held: dict, env: dict):
    """The act answered with who holds the loop; the tick it caused is a
    deny naming the person; no run exists on the ticket the loop would
    otherwise have implemented; the loop and the company both say so."""
    assert held["paused"]["paused"]["by"] == HUMAN, held["paused"]
    assert held["tick"]["decision"] == "deny", held["tick"]
    assert held["tick"]["rule"] == tick_rule(TICK_PAUSED, HUMAN), held["tick"]
    assert run_by_task(held["task"], workflow="implement") is None
    execution = console(env).heartbeat()["execution"]
    assert execution["status"] == "RUNNING" and execution["state"] == "paused", execution
    assert console(env).company()["paused"]["by"] == HUMAN


def test_the_pause_is_on_the_heartbeats_story_with_its_reason(held: dict):
    rows = [
        e
        for e in audit_story(f"heartbeat-{COMPANY}")
        if e["tool"] == HEARTBEAT_PAUSE and e["source"] == "console"
    ]
    assert rows, "no heartbeat.pause row under the heartbeat's run id"
    assert rows[-1]["agent_id"] == HUMAN and rows[-1]["decision"] == "allow"
    assert rows[-1]["rule"] == "heartbeat_pause"
    assert rows[-1]["rationale"] == "reading the ledger with nothing moving"


def test_a_wake_while_paused_ticks_paused_again_and_starts_nothing(held: dict):
    """The pause is the registry's fact, not the signal's: a nudge gets
    one more paused tick, never a start."""
    (tick,) = wake_and_await_ticks(1)
    assert tick["rule"] == tick_rule(TICK_PAUSED, HUMAN), tick
    assert run_by_task(held["task"], workflow="implement") is None


def test_a_person_the_company_did_not_name_cannot_lift_the_pause(held: dict, env: dict):
    auditor = console(env, name="auditor")
    with pytest.raises(PolicyDenied) as denied:
        auditor.resume("let it run")
    assert denied.value.rule == "human_not_recognized"
    refusals = [
        e
        for e in audit_story(f"heartbeat-{COMPANY}")
        if e["tool"] == HEARTBEAT_RESUME and e["agent_id"] == "auditor"
    ]
    assert refusals and refusals[-1]["decision"] == "deny"
    assert console(env).company()["paused"]["by"] == HUMAN


def test_resume_starts_the_held_work_and_a_run_in_flight_outlives_a_pause(
    held: dict, env: dict
):
    """Resume wakes the loop and the next tick starts the implement the
    pause held. A pause taken while that run is in flight touches it not
    at all: the run reaches its end, its end wakes the loop, and the loop
    ticks paused. The last resume leaves the loop asleep on its backlog."""
    mark = len(heartbeat_ticks(bare_heartbeat_id()))
    resumed = console(env).resume("read it; let the work go on")
    assert resumed["paused"] is None
    (tick,) = await_new_ticks(mark, 1)
    assert parse_tick_rule(tick["rule"])[0] == TICK_STARTED, tick
    started = await_run(held["task"], terminal=False, workflow="implement")
    assert started["status"] == "running", started

    mark = len(heartbeat_ticks(bare_heartbeat_id()))
    console(env).pause("holding while the run finishes")
    (tick,) = await_new_ticks(mark, 1)
    assert tick["rule"] == tick_rule(TICK_PAUSED, HUMAN), tick
    record = await_run(held["task"], terminal=True, workflow="implement")
    held["pr_url"] = record.get("pr_url")
    assert record["status"] == "done", record
    # The run's end nudged the loop, which read the pause again.
    (tick,) = await_new_ticks(mark + 1, 1)
    assert tick["rule"] == tick_rule(TICK_PAUSED, HUMAN), tick
    assert console(env).heartbeat()["execution"]["state"] == "paused"

    console(env).resume("done reading")
    await_bare_heartbeat_asleep()
    assert console(env).company()["paused"] is None
