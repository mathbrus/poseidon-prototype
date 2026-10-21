"""The happy path through Temporal: the implement workflow runs end-to-end on kind —
agent iterating in a sandbox, real PR on a `feature/*` branch — while the
orchestrator worker is killed mid-run. Green here proves the happy path
holds under a worker restart: the restarted worker resumes the workflow,
nothing leaks, and the terminal run record is honest.
"""

import re

import yaml

from .cluster import HELLO_SAAS, audit_story, sandbox_pods

# The branch pattern the applied blueprint declares — what the bounds
# tell the implementer and what the gateway enforces; never a literal of
# the suite's own.
BRANCH_PATTERN = yaml.safe_load(HELLO_SAAS.read_text())["policies"]["branch_protection"][
    "allowed_pattern"
]


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
    # Open at birth (verified by the fixture, which then closes it as suite
    # hygiene) and never merged: implement holds no merge authority.
    assert implement_run["pr_state_at_birth"] == "open"
    assert pr["merged"] is False
    assert re.fullmatch(BRANCH_PATTERN, pr["head"]["ref"]), (
        f"PR branch is {pr['head']['ref']!r}, not {BRANCH_PATTERN}"
    )
