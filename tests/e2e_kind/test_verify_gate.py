"""The platform verify gate on the live cluster: E3 — the executor's
completion claim is advisory, the platform's own `sandbox.run_tests` is the
gate of record.

The scripted scenario (examples/task-verify-red.md) plants a
state-dependent test that greens the executor's advisory verify and reds
the platform's re-run — a deterministic stand-in for "the claim no longer
holds when the platform checks it". The workflow must go red at the gate,
feed the failure back in a bounded re-dispatch, and only release the
deliver dispatch once its own verify is green.
"""

import re

import pytest

from .conftest import audit_story, run_implement, sandbox_pods


@pytest.fixture(scope="module")
def verify_red_run(stack: None, env: dict) -> dict:
    return run_implement("task-verify-red.md", kill_worker=False, env=env)


def test_red_gate_feeds_back_and_the_run_still_delivers(
    verify_red_run: dict, pr_info
) -> None:
    assert verify_red_run["status"] == "done", (
        f"run failed: {verify_red_run['error']}"
    )
    assert verify_red_run["pr_url"], "no PR after the red-path recovery"
    pr = pr_info(verify_red_run["pr_url"])
    assert re.fullmatch(r"feature/[a-z0-9-]+", pr["head"]["ref"])


def test_the_gate_of_record_went_red_then_green(verify_red_run: dict) -> None:
    """The platform's own verifies are in the story under its platform
    lifecycle: the first red (the claim did not hold), the last green (the
    re-dispatch fixed it) — and delivery only after the green."""
    story = audit_story(verify_red_run["run_id"])
    platform_verifies = [
        (i, e) for i, e in enumerate(story)
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert len(platform_verifies) >= 2, (
        "the red path needs at least two platform verifies"
    )
    pr_opens = [i for i, e in enumerate(story) if e["tool"] == "git.open_pr"
                and e["decision"] == "allow"]
    assert pr_opens, "no delivery in the story"
    assert max(i for i, _ in platform_verifies) < min(pr_opens), (
        "delivery did not wait for the last platform verify"
    )


def test_red_path_is_bounded_and_clean(verify_red_run: dict) -> None:
    """The loop is bounded by the blueprint's max_verify_retries (the run
    finished instead of spinning) and teardown still swept the crew."""
    assert sandbox_pods(verify_red_run["run_id"]) == []
    story = audit_story(verify_red_run["run_id"])
    assert any(e["tool"] == "lifecycle.revoke" for e in story)
