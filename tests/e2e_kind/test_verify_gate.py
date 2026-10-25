"""The platform verify gate on the live cluster: the implementer's
completion claim is advisory, the platform's own `sandbox.run_tests` is the
gate of record.

The injected ticket (`tickets/injected.md`) also carries the verify-red
marker, which plants a state-dependent test that greens the implementer's
advisory verify and reds the platform's re-run — a deterministic stand-in
for "the claim no longer holds when the platform checks it". The workflow
must go red at the gate, feed the failure back in a bounded re-dispatch,
and only release the deliver dispatch once its own verify is green.
"""

import yaml

from common.audit import LIFECYCLE_REVOKE

from .cluster import HELLO_SAAS, audit_story, sandbox_pods


def test_the_gate_of_record_went_red_then_green(injected_run: dict) -> None:
    """The platform's own verifies are in the story under its platform
    lifecycle: the first red (the claim did not hold), the last green (the
    re-dispatch fixed it) — and delivery only after the green."""
    story = audit_story(injected_run["run_id"])
    platform_verifies = [
        (i, e)
        for i, e in enumerate(story)
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert len(platform_verifies) >= 2, (
        "the red path needs at least two platform verifies"
    )
    pr_opens = [
        i
        for i, e in enumerate(story)
        if e["tool"] == "git.open_pr" and e["decision"] == "allow"
    ]
    assert pr_opens, "no delivery in the story"
    assert max(i for i, _ in platform_verifies) < min(pr_opens), (
        "delivery did not wait for the last platform verify"
    )


def test_red_path_is_bounded_and_clean(injected_run: dict) -> None:
    """The loop is bounded by the blueprint's `max_verify_retries`: the
    platform verified at most once per allowed re-dispatch plus the first
    — and teardown still swept the crew."""
    retries = yaml.safe_load(HELLO_SAAS.read_text())["workflows"]["implement"][
        "max_verify_retries"
    ]
    story = audit_story(injected_run["run_id"])
    platform_verifies = [
        e
        for e in story
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert 2 <= len(platform_verifies) <= retries + 1, platform_verifies
    assert sandbox_pods(injected_run["run_id"]) == []
    assert any(e["tool"] == LIFECYCLE_REVOKE for e in story)
