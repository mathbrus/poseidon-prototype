"""Unit tests for the review activities: config resolution
fail-closed, gate outcomes as first-class process-gate audit events, and
the post-gate merge as a gateway call. Sequencing — the merge strictly
after the gate — is a workflow concern, proven live by the kind suite."""

import asyncio
from typing import Any

import pytest

from orchestrator.activities.review import ReviewActivities
from orchestrator.config import WorkerSettings
from registry.memory import (
    InMemoryAuditStore,
    InMemoryCompanies,
    InMemoryRuns,
)

RUN_ID = "run-review-0001"

REVIEW_SPEC = {
    "namespace": "hello-saas",
    "workflows": [
        {
            "name": "implement",
            "enabled": True,
            "conductor": "pm",
            "crew": "feature-delivery",
            "max_verify_retries": 2,
        },
        {
            "name": "review",
            "enabled": True,
            "conductor": "pm",
            "crew": "feature-review",
            "gates": {"merge": {"resolved_by": "human"}},
        },
    ],
}

PM_ROW = {
    "agent_id": "pm-1",
    "company": "hello-saas",
    "hire": "pm",
    "role_template": "pm",
}


class FakeInstances:
    def __init__(self, rows):
        self.rows = rows

    def for_company(self, company):
        return [r for r in self.rows if r["company"] == company]


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
    )


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def _activities(settings, spec, instances, audit=None, runs=None):
    companies = InMemoryCompanies()
    if spec is not None:
        companies.upsert("hello-saas", spec)
    return ReviewActivities(settings, runs or InMemoryRuns(), companies, instances, audit)


def test_review_config_resolves_crew_conductor_and_gate(settings):
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    config = asyncio.run(activities.review_config())
    assert config == {
        "company": "hello-saas",
        "crew_template": "feature-review",
        "conductor": "pm-1",
        "conductor_role": "pm",
        "merge_gate": {"resolved_by": "human"},
    }


def test_review_config_carries_no_gate_when_none_is_declared(settings):
    spec = {
        "workflows": [
            {
                "name": "review",
                "enabled": True,
                "conductor": "pm",
                "crew": "feature-review",
            },
        ],
    }
    activities = _activities(settings, spec, FakeInstances([PM_ROW]))
    assert asyncio.run(activities.review_config())["merge_gate"] is None


def test_review_config_fails_closed_without_a_blueprint(settings):
    activities = _activities(settings, None, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="no enabled"):
        asyncio.run(activities.review_config())


def test_review_config_fails_closed_without_a_crew(settings):
    spec = {
        "workflows": [
            {"name": "review", "enabled": True, "conductor": "pm", "crew": None}
        ]
    }
    activities = _activities(settings, spec, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="binds no crew"):
        asyncio.run(activities.review_config())


def test_review_config_fails_closed_without_a_conductor_instance(settings):
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([]))
    with pytest.raises(RuntimeError, match="no standing instance"):
        asyncio.run(activities.review_config())


def test_create_review_run_opens_a_review_row(settings):
    runs = InMemoryRuns()
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), runs=runs)
    asyncio.run(activities.create_review_run(RUN_ID, 41))
    record = runs.get(RUN_ID)
    assert record.status == "running"
    assert record.workflow == "review"
    assert record.workflow_id == f"review-{RUN_ID}"
    assert "#41" in record.task


def test_gate_outcomes_are_process_gate_audit_events(settings, audit):
    """Escalation and the verdict are first-class rows carrying the
    workflow id that maps back to run_id — approved is the only allow."""
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit=audit)
    for kind in ("escalated", "approved"):
        asyncio.run(
            activities.record_gate_outcome(RUN_ID, "merge", kind, f"review-{RUN_ID}")
        )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.rule, e.decision) for e in events] == [
        ("gate_escalated", "deny"),
        ("gate_approved", "allow"),
    ]
    assert all(e.source == "process-gate" for e in events)
    assert all(e.tool == "gate.merge" for e in events)
    assert all(e.workflow == f"review-{RUN_ID}" for e in events)


def test_verdicts_are_journaled_audit_events(settings, audit):
    """The reviewer's structured verdict becomes an append-only
    process-lifecycle row — the conduct.decide idiom: pass maps to allow,
    fail to deny, and the one-line summary rides the rule field."""
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]), audit)
    asyncio.run(
        activities.record_verdict(
            RUN_ID, "pass", "Suite green; in scope.", f"review-{RUN_ID}"
        )
    )
    asyncio.run(
        activities.record_verdict(
            RUN_ID, "fail", "Blocking: criteria unmet.", f"review-{RUN_ID}"
        )
    )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.decision, e.rule) for e in events] == [
        ("allow", "Suite green; in scope."),
        ("deny", "Blocking: criteria unmet."),
    ]
    assert all(e.source == "process-lifecycle" for e in events)
    assert all(e.tool == "review.verdict" for e in events)
    assert all(e.workflow == f"review-{RUN_ID}" for e in events)


def test_record_verdict_requires_an_audit_store(settings):
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="audit store"):
        asyncio.run(activities.record_verdict(RUN_ID, "pass", "s", "wf"))


def test_read_pr_keeps_the_shape_and_drops_the_patches(settings, monkeypatch):
    import orchestrator.activities.review as module

    class FakeGateway:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read_pr(self, run_id, repo, number):
            return {
                "number": number,
                "title": "Add slugify",
                "head": "feature/slugify-x",
                "url": "https://pr/41",
                "files": [{"path": "slugify.py", "patch": "+..."}],
            }

    monkeypatch.setattr(module, "GatewayClient", FakeGateway)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    pr = asyncio.run(activities.read_pr(RUN_ID, 41))
    assert pr["head"] == "feature/slugify-x"
    assert "files" not in pr


def test_read_pr_refuses_a_headless_pr(settings, monkeypatch):
    """No head ref means nothing to provision the review sandbox from
    — refuse before provisioning, like every config activity."""
    import orchestrator.activities.review as module

    class FakeGateway:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read_pr(self, run_id, repo, number):
            return {"number": number, "head": ""}

    monkeypatch.setattr(module, "GatewayClient", FakeGateway)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    with pytest.raises(RuntimeError, match="no head ref"):
        asyncio.run(activities.read_pr(RUN_ID, 41))


def test_merge_pr_raises_when_the_merge_does_not_land(settings, monkeypatch):
    import orchestrator.activities.review as module

    merged: list[dict[str, Any]] = [
        {"merged": True, "sha": "abc123"},
        {"merged": False, "sha": ""},
    ]

    class FakeGateway:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def merge_pr(self, run_id, repo, number, method="merge"):
            return merged.pop(0)

    monkeypatch.setattr(module, "GatewayClient", FakeGateway)
    activities = _activities(settings, REVIEW_SPEC, FakeInstances([PM_ROW]))
    assert asyncio.run(activities.merge_pr(RUN_ID, 41))["sha"] == "abc123"
    with pytest.raises(RuntimeError, match="did not land"):
        asyncio.run(activities.merge_pr(RUN_ID, 41))
