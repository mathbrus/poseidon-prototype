"""E4 — audit: the happy-path story is reconstructable from the JSONL alone.

Every gateway call of the E1 run appears as a valid C1 event, in append
order, with non-empty digests — from the runner registering the sandbox,
through the agent's survey and test runs, to the bundle and the PR.
"""

import re

from common.audit import AuditEvent
from runner.config import RunnerSettings
from runner.flow import Manifest

from tests.e2e.conftest import audit_events

SHA256_HEX = re.compile(r"[0-9a-f]{64}")


def test_e1_story_reconstructable_from_audit_log(
    happy_run: Manifest, settings: RunnerSettings
) -> None:
    raw = audit_events(settings, happy_run.run_id)
    assert raw, "no audit events for the happy run"
    events = [AuditEvent.model_validate(event) for event in raw]  # C1 schema holds

    for event in events:
        assert SHA256_HEX.fullmatch(event.args_digest), "empty or malformed digest"
        assert event.rule, "every decision names its rule"
        assert event.latency_ms >= 0

    # The story, in order. Sandbox registration comes first, by the runner...
    assert events[0].tool == "/admin/sandboxes"
    assert (events[0].agent_id, events[0].role) == ("runner", "runner")
    assert events[0].decision == "allow"

    # ...everything after is the agent working through the gateway...
    tools = [event.tool for event in events]
    for event in events[1:]:
        assert (event.agent_id, event.role) == ("backend-engineer-1", "backend-engineer")
        assert event.tool in {
            "sandbox.read_file", "sandbox.write_file", "sandbox.list_dir",
            "sandbox.exec", "sandbox.run_tests", "sandbox.git_bundle", "git.open_pr",
        }, f"unexpected tool in the run story: {event.tool}"

    # ...starting with the plan survey, verified at least once, and delivered
    # as bundle-then-PR, with nothing after the PR.
    assert tools[1] == "sandbox.list_dir", "the plan survey should open the story"
    assert "sandbox.run_tests" in tools
    assert tools[-2:] == ["sandbox.git_bundle", "git.open_pr"]
    assert events[-1].decision == "allow"
    assert events[-1].backend_status == "ok"

    # E1 contains no denial (E2 owns the deny story)...
    assert all(event.decision == "allow" for event in events)

    # ...and the manifest's audit-line count matches the log.
    assert happy_run.audit_lines == len(events)
