"""The intake activities against doubles: the run row, the
budget-headroom boundary, the blueprint-read gate slot, the PM dispatch,
and finalization linking tickets."""

import asyncio

import httpx
import pytest

from orchestrator.activities.intake import IntakeActivities
from orchestrator.config import WorkerSettings
from registry.memory import (
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRuns,
)

RUN_ID = "run-01INTAKE"


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token = tmp_path / "token"
    token.write_text("projected")
    return WorkerSettings(
        gateway_token_file=str(token),
        mcp_gateway_url="http://gateway.test:8080",
        pm_url="http://pm.test:8000",
        target_repo="github.com/example/repo",
        poll_interval_s=0.01,
    )


@pytest.fixture
def companies() -> InMemoryCompanies:
    store = InMemoryCompanies()
    store.upsert(
        "hello-saas",
        {
            "namespace": "hello-saas",
            "budgets": {"per_run_usd": 5.0},
            "workflows": [
                {"name": "intake"},
                {"name": "review", "gates": {"merge": {"resolved_by": "human"}}},
            ],
        },
    )
    return store


@pytest.fixture
def world(settings, companies):
    runs = InMemoryRuns()
    ledger = InMemoryBudgetLedger()
    return IntakeActivities(settings, runs, companies, ledger), runs, ledger


def test_create_and_finalize_link_tickets(world):
    intake, runs, _ = world
    asyncio.run(intake.create_intake_run(RUN_ID, "ship a slug endpoint"))
    record = runs.get(RUN_ID)
    assert record.workflow == "intake"
    assert record.workflow_id == f"intake-{RUN_ID}"
    assert record.company == "hello-saas"

    result = {
        "status": "done",
        "ticket_urls": ["https://github.test/i/7"],
        "crew_request_id": "crq-1",
        "iterations": 0,
        "error": None,
    }
    final = asyncio.run(intake.finalize_intake(RUN_ID, result, None))
    assert final["status"] == "done"
    assert final["ticket_urls"] == ["https://github.test/i/7"]


def test_budget_boundary_fails_born_exhausted_runs(world):
    intake, _, ledger = world
    headroom = asyncio.run(intake.check_budget(RUN_ID))
    assert headroom == {"cap_usd": 5.0, "spent_usd": 0.0}

    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="pm-1",
        parent_agent_id="",
        role="pm",
        model="mock-brain",
        prompt_tokens=1,
        completion_tokens=1,
        usd=5.0,
    )
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        asyncio.run(intake.check_budget(RUN_ID))


def test_gate_config_reads_the_applied_blueprint(world, companies):
    intake, _, _ = world
    assert asyncio.run(intake.intake_gate_config()) == {}

    spec = companies.get("hello-saas")
    spec["workflows"][0]["gates"] = {"intake": {"resolved_by": "human"}}
    companies.upsert("hello-saas", spec)
    assert asyncio.run(intake.intake_gate_config()) == {
        "intake": {"resolved_by": "human"}
    }


def test_dispatch_pm_polls_to_terminal(world, monkeypatch):
    intake, _, _ = world
    statuses = [
        {"status": "running"},
        {
            "status": "done",
            "pr_url": None,
            "iterations": 0,
            "error": None,
            "ticket_urls": ["https://github.test/i/7"],
            "crew_request_id": "crq-1",
        },
    ]

    def handler(request):
        if request.method == "POST":
            return httpx.Response(202, json={})
        return httpx.Response(
            200, json=statuses.pop(0) if len(statuses) > 1 else statuses[0]
        )

    from orchestrator import clients

    original_init = clients.AgentClient.__init__

    def patched(self, url: str) -> None:
        assert url == "http://pm.test:8000"  # the PM, not the engineer
        original_init(self, url)
        self._http = httpx.AsyncClient(
            base_url=url, transport=httpx.MockTransport(handler)
        )

    monkeypatch.setattr(clients.AgentClient, "__init__", patched)
    status = asyncio.run(intake.dispatch_pm(RUN_ID, "goal"))
    assert status["ticket_urls"] == ["https://github.test/i/7"]
    assert status["crew_request_id"] == "crq-1"
