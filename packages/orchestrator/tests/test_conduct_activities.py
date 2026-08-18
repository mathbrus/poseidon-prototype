"""Unit tests for the conduct activities: observation, authorization and
the audit trail, against fakes. The tick's sequencing, the sleep cadence
and the child start are workflow concerns, proven live by the kind e2e."""

import asyncio
from typing import Any

import pytest
from temporalio.exceptions import ApplicationError

from orchestrator.activities.conduct import ConductActivities
from orchestrator.config import WorkerSettings
from registry.memory import (
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRuns,
)

COMPANY = "hello-saas"

CONDUCT_SPEC = {
    "conductor": "pm",
    "heartbeat_minutes": 30,
    "max_concurrent_runs": 1,
    "may_start": ["implement"],
    "daily_cap_usd": 20.0,
}


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
        company=COMPANY,
    )


@pytest.fixture
def runs() -> InMemoryRuns:
    return InMemoryRuns()


@pytest.fixture
def companies() -> InMemoryCompanies:
    return InMemoryCompanies()


@pytest.fixture
def ledger() -> InMemoryBudgetLedger:
    return InMemoryBudgetLedger()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def activities(settings, runs, companies, ledger, audit) -> ConductActivities:
    return ConductActivities(settings, runs, companies, ledger, audit)


def _backlog_gateway(monkeypatch, issues: list[dict[str, Any]]):
    import orchestrator.activities.conduct as module

    class FakeGateway:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read_open_issues(self, run_id, repo):
            return issues

    monkeypatch.setattr(module, "GatewayClient", FakeGateway)


def _opa(monkeypatch, allow: bool, rule: str):
    import orchestrator.activities.conduct as module

    class FakeResponse:
        def raise_for_status(self):
            return None

        def json(self):
            return {"result": {"allow": allow, "rule": rule}}

    class FakeHttp:
        def __init__(self, *a, **k): ...

        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, url, json):
            self.url, self.input = url, json["input"]
            FakeHttp.last = self
            return FakeResponse()

    monkeypatch.setattr(module.httpx, "AsyncClient", FakeHttp)
    return FakeHttp


# -- conduct_config --------------------------------------------------------


def test_config_returns_the_applied_section(activities, companies):
    companies.upsert(COMPANY, {"conduct": CONDUCT_SPEC})
    assert asyncio.run(activities.conduct_config()) == CONDUCT_SPEC


def test_config_is_none_without_a_section(activities, companies):
    """The fail-closed half of revocation: an applied blueprint without
    the section stops the loop at its next tick."""
    companies.upsert(COMPANY, {"workflows": []})
    assert asyncio.run(activities.conduct_config()) is None
    companies.delete(COMPANY)
    assert asyncio.run(activities.conduct_config()) is None


# -- conduct_observe -------------------------------------------------------


def _issue(number: int) -> dict[str, Any]:
    return {
        "number": number,
        "title": f"issue {number}",
        "url": f"https://github.test/example/repo/issues/{number}",
        "body": "UNTRUSTED — must not leave the observation",
    }


def test_observe_picks_the_oldest_unconsumed_issue(activities, runs, ledger, monkeypatch):
    _backlog_gateway(monkeypatch, [_issue(7), _issue(3), _issue(5)])
    runs.start("run-a", "implement issue #3", "implement", company=COMPANY)
    observed = asyncio.run(activities.conduct_observe())
    assert observed["next_issue"]["number"] == 5
    assert observed["open_candidates"] == 2
    # Bodies are untrusted input; only number, title and URL feed the tick.
    assert set(observed["next_issue"]) == {"number", "title", "url"}


def test_observe_counts_running_delivery_runs_and_todays_spend(
    activities, runs, ledger, monkeypatch
):
    _backlog_gateway(monkeypatch, [])
    runs.start("run-a", "implement issue #3", "implement", company=COMPANY)
    runs.start("run-b", "goal", "intake", company=COMPANY)  # not capacity
    runs.finish("run-c", "done")
    ledger.record(
        company=COMPANY,
        run_id="run-a",
        agent_id="executor",
        parent_agent_id="pm-1",
        role="backend-engineer",
        model="mock",
        prompt_tokens=1,
        completion_tokens=1,
        usd=2.5,
    )
    observed = asyncio.run(activities.conduct_observe())
    assert observed["active_runs"] == 1
    assert observed["daily_spent_usd"] == 2.5
    assert observed["next_issue"] is None


def test_observe_exhausted_backlog_offers_nothing(activities, runs, monkeypatch):
    """A failed issue-run is a recorded outcome, not an invitation to
    retry forever: every consumed issue stays consumed."""
    _backlog_gateway(monkeypatch, [_issue(3)])
    runs.start("run-a", "implement issue #3", "implement", company=COMPANY)
    runs.finish("run-a", "failed", error="boom")
    observed = asyncio.run(activities.conduct_observe())
    assert observed["next_issue"] is None


# -- conduct_authorize -----------------------------------------------------


def test_authorize_allow_is_audited_and_returns(activities, audit, monkeypatch):
    http = _opa(monkeypatch, allow=True, rule="conduct_start_implement")
    asyncio.run(activities.conduct_authorize("auto-1", "implement", 0, 3.5))
    assert "/v1/data/poseidon/authz/hello_saas/decision" in http.last.url
    assert http.last.input["tool"] == "conduct.start"
    assert http.last.input["lifecycle"] == "platform"
    assert http.last.input["args"] == {
        "workflow": "implement",
        "active_runs": 0,
        "daily_spent_usd": 3.5,
    }
    (event,) = audit.story("auto-1")
    assert event["decision"] == "allow"
    assert event["tool"] == "conduct.start"
    assert event["rule"] == "conduct_start_implement"


def test_authorize_deny_raises_nonretryable_and_audits(activities, audit, monkeypatch):
    _opa(monkeypatch, allow=False, rule="conduct_start_over_daily_cap")
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(activities.conduct_authorize("auto-2", "implement", 0, 20.0))
    assert caught.value.non_retryable
    assert "conduct_start_over_daily_cap" in str(caught.value)
    (event,) = audit.story("auto-2")
    assert event["decision"] == "deny"


# -- record_conduct_tick ---------------------------------------------------


def test_every_tick_leaves_a_correlated_row(activities, audit):
    asyncio.run(activities.record_conduct_tick("idle", "backlog_empty", "conduct-x"))
    asyncio.run(
        activities.record_conduct_tick("started", "implement_issue_5", "conduct-x")
    )
    story = audit.story(f"conduct-{COMPANY}")
    # The gate-outcome idiom: started is the tick's allow, idle its deny.
    assert [e["decision"] for e in story] == ["deny", "allow"]
    assert [e["rule"] for e in story] == [
        "tick_idle:backlog_empty",
        "tick_started:implement_issue_5",
    ]
    assert {e["tool"] for e in story} == {"conduct.tick"}
    assert {e["source"] for e in story} == {"process-lifecycle"}
