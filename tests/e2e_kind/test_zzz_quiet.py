"""The suite's exit contract: quiet, asserted (#15 step 3).

"Suite finished" and "cluster quiet" used to differ by up to
`heartbeat_minutes`: a module that applied a conduct-declaring blueprint
left the ensure loop's bare heartbeat asleep behind it, and a killed run
left workflows parked on day-long gates. Both were found by a person
asking "is something still running?" — the wrong detector. This module
collects last (`zzz`) and makes the quiet a failing assertion instead:
when the suite exits green, nothing is in flight anywhere the platform
can account for it.

Scope, honestly: workflows and runs, not repo debris — the PR sweep is
the session sweeper's teardown, which runs *after* the last test by
construction, so it cannot be asserted from inside one. And the standing
PM deployment is not in scope because it is supposed to exist: quiet
means no *work* in flight, not no company.
"""

import subprocess

from .conftest import PLATFORM_NS, POSTGRES_USER


def _running_workflows() -> list[str]:
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "exec",
            "-i",
            "deploy/temporal-admintools",
            "--",
            "temporal",
            "workflow",
            "list",
            "--address",
            "temporal-frontend:7233",
            "--query",
            "ExecutionStatus='Running'",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"temporal list failed: {proc.stderr}"
    return [
        line.strip()
        for line in proc.stdout.splitlines()
        if line.strip() and not line.strip().startswith("Status")
    ]


def test_no_workflow_is_still_running(stack: None):
    """Every workflow the session started — or caused the ensure loop to
    start — has terminated: no heartbeat sleeping toward its next tick, no
    review parked on a gate nobody will answer."""
    assert _running_workflows() == [], (
        "workflows survived the suite; a module left its campsite dirty"
    )


def test_no_run_is_left_non_terminal(stack: None):
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
            "SELECT COALESCE(json_agg(json_build_object("
            "'run_id', r.run_id, 'workflow', r.workflow, 'status', r.status)), '[]') "
            "FROM registry.runs r WHERE r.status NOT IN ('done', 'failed')",
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"psql failed: {proc.stderr}"
    leftovers = proc.stdout.strip()
    assert leftovers == "[]", f"runs left non-terminal: {leftovers}"
