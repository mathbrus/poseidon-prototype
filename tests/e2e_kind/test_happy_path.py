"""The happy path through Temporal: the implement workflow runs end-to-end on kind —
agent iterating in a sandbox, real PR on a `feature/*` branch — while the
orchestrator worker is killed mid-run, with the crew's pods up. Green here
proves the happy path holds under a worker restart: the restarted worker
resumes the workflow, nothing leaks — the sandbox, the ephemeral
implementer's identity and its pod are all swept by the compensation
Temporal replays — and the terminal run record is honest.

The implementer leaves its work uncommitted, and the platform makes the
delivery commit: the PR carries exactly the files it verified, in one
commit the implementer is the author of, and nothing the sandbox keeps
for itself (`.poseidon-base`, bytecode).
"""

import re
import time

import yaml

from common.audit import LIFECYCLE_DESTROY_POD, LIFECYCLE_REVOKE
from orchestrator.ephemeral_pods import pod_name
from orchestrator.identity import ephemeral_agent_id

from .cluster import (
    COMPANY_NS,
    HELLO_SAAS,
    audit_story,
    participants,
    run_kubectl,
    sandbox_pods,
)

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


def test_the_worker_kill_leaked_no_crew(implement_run: dict) -> None:
    """Identities revoked and pods gone after a teardown that rode the
    workflow's compensation through a worker kill."""
    run_id = implement_run["run_id"]
    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", run_id)
    crew = {agent.agent_id: agent.status for agent in participants(run_id)}
    assert implementer_id in crew, crew
    assert all(status == "terminated" for status in crew.values()), crew
    # "Survived" means still there, not still there *this instant*. A
    # cancelled fulfillment retry can be mid-`provision` in a worker thread
    # when the workflow returns — `asyncio.to_thread` cannot interrupt it —
    # so a doomed pod may appear for a few seconds and then be collected.
    # The bound keeps this honest: the leak this test exists to catch — a
    # provision landing past teardown with nothing left to collect it —
    # leaves pods Running indefinitely, so a real leak still fails here.
    deadline = time.monotonic() + 90
    while True:
        pod = run_kubectl(
            "-n",
            COMPANY_NS,
            "get",
            "pod",
            pod_name(implementer_id),
            "-o",
            "name",
            timeout=30,
        )
        if pod.returncode != 0:
            assert "NotFound" in pod.stderr, pod.stderr
            break
        assert time.monotonic() < deadline, (
            f"the ephemeral pod survived teardown: {pod.stdout.strip()}"
        )
        time.sleep(3)
    story = audit_story(run_id)
    assert any(e["tool"] == LIFECYCLE_REVOKE for e in story)
    assert any(e["tool"] == LIFECYCLE_DESTROY_POD for e in story)


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
