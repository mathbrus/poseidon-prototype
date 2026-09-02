"""The intake activities against doubles: the run row, the
budget-headroom boundary, the blueprint-read intake shape (gates and the
conductor's address), and finalization linking tickets."""

import asyncio

import pytest

from orchestrator.activities.intake import IntakeActivities
from orchestrator.config import WorkerSettings
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRuns,
)

RUN_ID = "run-01INTAKE"


def settings(tmp_path) -> WorkerSettings:
    token = tmp_path / "token"
    token.write_text("projected")
    return WorkerSettings(
        gateway_token_file=str(token),
        mcp_gateway_url="http://gateway.test:8080",
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
                {"name": "intake", "enabled": True, "conductor": "pm"},
                {"name": "review", "gates": {"merge": {"resolved_by": "human"}}},
            ],
        },
    )
    return store


@pytest.fixture
def instances() -> InMemoryAgentInstances:
    store = InMemoryAgentInstances()
    store.upsert("pm-1", "hello-saas", "pm", "pm", "standard", namespace="hello-saas")
    return store


@pytest.fixture
def world(settings, companies, instances):
    runs = InMemoryRuns()
    ledger = InMemoryBudgetLedger()
    return IntakeActivities(settings, runs, companies, instances, ledger), runs, ledger


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
        cached_tokens=0,
        reasoning_tokens=0,
        usd=5.0,
    )
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        asyncio.run(intake.check_budget(RUN_ID))


def test_intake_config_reads_gates_and_the_conductor_address(world, companies):
    """The gate slot reads config, never hardcodes; the conductor's address
    is derived from the converged instance, never configured."""
    intake, _, _ = world
    config = asyncio.run(intake.intake_config())
    assert config == {
        "gates": {},
        "conductor_url": "http://pm.hello-saas.svc.cluster.local:8000",
    }

    spec = companies.get("hello-saas")
    spec["workflows"][0]["gates"] = {"intake": {"resolved_by": "human"}}
    companies.upsert("hello-saas", spec)
    assert asyncio.run(intake.intake_config())["gates"] == {
        "intake": {"resolved_by": "human"}
    }


def test_intake_config_fails_closed_without_a_conductor_instance(settings, companies):
    intake = IntakeActivities(
        settings,
        InMemoryRuns(),
        companies,
        InMemoryAgentInstances(),
        InMemoryBudgetLedger(),
    )
    with pytest.raises(RuntimeError, match="no standing instance"):
        asyncio.run(intake.intake_config())
