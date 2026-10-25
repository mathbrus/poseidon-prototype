"""The happy path through Temporal: the implement workflow runs end-to-end on kind —
agent iterating in a sandbox, real PR on a `feature/*` branch — while the
orchestrator worker is killed mid-run. Green here proves the happy path
holds under a worker restart: the restarted worker resumes the workflow,
nothing leaks, and the terminal run record is honest.

The implementer leaves its work uncommitted, and the platform makes the
delivery commit: the PR carries exactly the files it verified, in one
commit the implementer is the author of, and nothing the sandbox keeps
for itself (`.poseidon-base`, bytecode).
"""

import re

import yaml

from orchestrator.identity import ephemeral_agent_id

from .cluster import HELLO_SAAS, sandbox_pods

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


def test_pr_is_on_feature_branch(implement_run: dict, pr_info) -> None:
    pr = pr_info(implement_run["pr_url"])
    # Open at birth (verified by the fixture, which then closes it as suite
    # hygiene) and never merged: implement holds no merge authority.
    assert implement_run["pr_state_at_birth"] == "open"
    assert pr["merged"] is False
    assert re.fullmatch(BRANCH_PATTERN, pr["head"]["ref"]), (
        f"PR branch is {pr['head']['ref']!r}, not {BRANCH_PATTERN}"
    )


def test_the_delivery_is_the_verified_workspace(implement_run: dict) -> None:
    """The mock implementer writes `slugify.py` and its unit tests and
    commits nothing. The delivery is those files where they differ from
    main — the implementation always does, its branch stamped in — and
    nothing the platform's own verify left in the sandbox."""
    files = implement_run["pr_files"]
    assert "slugify.py" in files, files
    assert set(files) <= {"slugify.py", "tests/unit/test_slugify.py"}, files


def test_the_platform_made_the_one_delivery_commit(implement_run: dict) -> None:
    (commit,) = implement_run["pr_commits"]
    author = ephemeral_agent_id(
        "feature-delivery", "implementer", implement_run["run_id"]
    )
    assert commit["commit"]["author"]["name"] == author
