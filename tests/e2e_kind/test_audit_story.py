"""E4 on the cluster (plan step 6): the whole story of a run is
reconstructable by `run_id` alone — every enforcement decision the platform
made, in order, from one query. Since plan step 8 that is literally one
query: the gateway and the egress proxy both write the registry
`audit_events` table.
"""

from .conftest import audit_story


def test_story_covers_the_whole_run(implement_run: dict) -> None:
    story = audit_story(implement_run["run_id"])
    assert story, "no audit events for the run"

    tools = {e["tool"] for e in story}
    # The run's arc: sandbox registered, workspace fetched, files written,
    # tests run, work committed, bundle pulled, PR opened.
    assert {
        "/admin/sandboxes",
        "repo.archive",
        "sandbox.write_file",
        "sandbox.exec",
        "sandbox.run_tests",
        "sandbox.git_bundle",
        "git.open_pr",
    } <= tools, f"story is missing steps: {sorted(tools)}"


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
