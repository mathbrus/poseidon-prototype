"""E1 through Temporal (plan steps 1d + 3): the implement workflow runs
end-to-end on kind — agent iterating in a sandbox, real PR on a `feature/*`
branch — while the orchestrator worker is killed mid-run. Green here proves
both the E1 happy path and the step-3 done-when: the restarted worker
resumes the workflow, nothing leaks, and the terminal run record is honest.
"""

import re

from .conftest import audit_story, sandbox_pods


def test_run_completes_with_pr(implement_run: dict) -> None:
    assert implement_run["status"] == "done", f"run failed: {implement_run['error']}"
    assert implement_run["pr_url"], "no PR URL in the run record"
    assert implement_run["iterations"] >= 1


def test_no_leaked_sandbox(implement_run: dict) -> None:
    assert sandbox_pods(implement_run["run_id"]) == []


def test_audit_story_on_cluster(implement_run: dict) -> None:
    events = audit_story(implement_run["run_id"])
    assert events, "no audit events for the run in the registry"


def test_pr_is_on_feature_branch(implement_run: dict, pr_info) -> None:
    pr = pr_info(implement_run["pr_url"])
    assert pr["state"] == "open"
    assert re.fullmatch(r"feature/[a-z0-9-]+", pr["head"]["ref"]), (
        f"PR branch is {pr['head']['ref']!r}, not feature/*"
    )
