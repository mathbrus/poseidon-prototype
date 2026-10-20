"""The suite's exit contract: quiet, asserted.

"Suite finished" and "cluster quiet" can differ: a killed run leaves
workflows parked on day-long gates, and a module that left backlog behind
leaves the standing heartbeat working it. A person asking "is something
still running?" is the wrong detector for either. The conftest orders this
module last and makes the quiet a failing assertion instead: when the
suite exits green, nothing is in flight anywhere the platform can account
for it — except the company's own heartbeat, which is supposed to run,
asleep on an empty backlog.

Scope, honestly: workflows and runs, not repo debris — the PR sweep is
the session sweeper's teardown, which runs *after* the last test by
construction, so it cannot be asserted from inside one. And the standing
PM deployment is not in scope because it is supposed to exist: quiet
means no *work* in flight, not no company.
"""

from common.workflows import TICK_STARTED, parse_tick_rule

from .cluster import COMPANY, non_terminal_runs, running_workflows


def test_only_the_heartbeat_is_still_running(stack: None):
    """Every run-shaped workflow the session caused has terminated: no
    review parked on a gate nobody will answer, no scope run mid-dispatch.
    The one workflow left is the standing heartbeat — a company always has
    one — and it is asleep, its last tick idle on an empty backlog."""
    from .cluster import await_bare_heartbeat_asleep, bare_heartbeat_id, heartbeat_ticks

    running = [w for w in running_workflows() if f"heartbeat-{COMPANY}" not in w]
    assert running == [], "workflows survived the suite; a module left its campsite dirty"
    await_bare_heartbeat_asleep()
    ticks = heartbeat_ticks(bare_heartbeat_id())
    assert ticks and ticks[-1]["decision"] == "deny", ticks[-1:]
    assert parse_tick_rule(ticks[-1]["rule"] or "")[0] != TICK_STARTED, ticks[-1]


def test_no_run_is_left_non_terminal(stack: None):
    leftovers = non_terminal_runs()
    assert leftovers == [], f"runs left non-terminal: {leftovers}"
