"""The suite's exit contract: quiet, asserted.

"Suite finished" and "cluster quiet" can differ by up to
`heartbeat_minutes`: a module that applies a conduct-declaring blueprint
leaves the ensure loop's bare heartbeat asleep behind it, and a killed run
leaves workflows parked on day-long gates. A person asking "is something
still running?" is the wrong detector for either. The conftest orders this
module last and makes the quiet a failing assertion instead: when the
suite exits green, nothing is in flight anywhere the platform can account
for it.

Scope, honestly: workflows and runs, not repo debris — the PR sweep is
the session sweeper's teardown, which runs *after* the last test by
construction, so it cannot be asserted from inside one. And the standing
PM deployment is not in scope because it is supposed to exist: quiet
means no *work* in flight, not no company.
"""

from .cluster import non_terminal_runs, running_workflows


def test_no_workflow_is_still_running(stack: None):
    """Every workflow the session started — or caused the ensure loop to
    start — has terminated: no heartbeat sleeping toward its next tick, no
    review parked on a gate nobody will answer."""
    assert running_workflows() == [], (
        "workflows survived the suite; a module left its campsite dirty"
    )


def test_no_run_is_left_non_terminal(stack: None):
    leftovers = non_terminal_runs()
    assert leftovers == [], f"runs left non-terminal: {leftovers}"
