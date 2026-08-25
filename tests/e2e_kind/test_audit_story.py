"""The audit story on the cluster: the whole story of a run is reconstructable by
`run_id` alone — every enforcement decision the platform made, in order,
from one query. That is literally one query: the gateway and the egress
proxy both write the registry `audit_events` table.
"""

from .cluster import audit_story


def _executor_id(run_id: str) -> str:
    from orchestrator.identity import ephemeral_agent_id

    return ephemeral_agent_id("feature-delivery", "executor", run_id)


def test_story_covers_the_whole_run(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    assert story, "no audit events for the run"

    tools = {e["tool"] for e in story}
    # The run's whole arc: sandbox registered, workspace fetched,
    # the crew stood up (request recorded under the conductor's authority,
    # agents bound, their pods provisioned, fulfillment), files
    # written, tests run, work committed, bundle pulled, PR opened, and the
    # crew revoked at teardown.
    assert {
        "/admin/sandboxes",
        "repo.archive",
        "lifecycle.record_request",
        "lifecycle.bind",
        "lifecycle.provision_pod",
        "lifecycle.fulfill",
        "sandbox.write_file",
        "sandbox.exec",
        "sandbox.run_tests",
        "sandbox.git_bundle",
        "git.open_pr",
        "lifecycle.revoke",
    } <= tools, f"story is missing steps: {sorted(tools)}"


def test_delivery_never_precedes_the_platform_verify(implement_run: dict) -> None:
    """The verify gate of record, in the story's own ordering: the
    platform's green sandbox.run_tests (the orchestrator's platform
    lifecycle) comes before the PR ever opens — delivery is released by
    the gate, not by the executor's claim."""
    story = audit_story(implement_run["run_id"])
    platform_verifies = [
        i
        for i, e in enumerate(story)
        if e["tool"] == "sandbox.run_tests" and e["lifecycle"] == "platform"
    ]
    assert platform_verifies, "no platform-identity verify in the story"
    pr_opens = [i for i, e in enumerate(story) if e["tool"] == "git.open_pr"]
    assert pr_opens, "no delivery in the story"
    assert min(platform_verifies) < min(pr_opens), (
        "delivery happened before the platform verify gate"
    )


def test_every_event_is_attributed_and_decided(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    for event in story:
        assert event["run_id"] == implement_run["run_id"]
        assert event["decision"] in ("allow", "deny")
        assert event["agent_id"]
        assert event["ts"]


def test_happy_path_story_contains_no_denies(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    denies = [e for e in story if e["decision"] == "deny"]
    assert denies == [], f"unexpected denies in the happy path: {denies}"


def test_one_query_spans_the_runs_real_sources(implement_run: dict) -> None:
    """The one-ingestion-contract claim on real producers only — nothing
    seeded. A
    happy crewed implement legitimately produces exactly these three
    sources (no gate parks, nothing attempts egress); the sources a happy
    run *cannot* produce are proven on the runs that do produce them —
    process-gate on the review chain and the budget pause, sandbox-infra
    on the egress/red-team runs — and all five together, still with no
    seeding, in test_acceptance."""
    run_id = implement_run["run_id"]
    story = audit_story(run_id)
    assert {e["source"] for e in story} == {
        "gateway",
        "llm-gateway",
        "process-lifecycle",
    }
    assert all(e["run_id"] == run_id for e in story)


def test_gateway_events_carry_full_attribution(implement_run: dict) -> None:
    """The real pipeline's events are fully attributed: every gateway event names
    its source, and company-caller events carry company + lifecycle."""
    run_id = implement_run["run_id"]
    story = [e for e in audit_story(run_id) if e["source"] == "gateway"]
    assert story
    for event in story:
        assert event["lifecycle"] in ("standing", "ephemeral", "platform")
    executor_events = [e for e in story if e["agent_id"] == _executor_id(run_id)]
    assert executor_events
    for event in executor_events:
        assert event["company"] == "hello-saas"
        assert event["lifecycle"] == "ephemeral"
        assert event["parent_agent_id"] == "pm-1"
        assert event["workflow"] == "implement"
