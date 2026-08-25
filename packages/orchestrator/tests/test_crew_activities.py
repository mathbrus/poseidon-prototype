"""Identity binding against doubles: bind writes one deterministic
participants row per {crew, member, run} — the row *is* the identity the
gateways resolve a crew ServiceAccount against; revoke sweeps every live
participant; both land process-lifecycle audit events; a retried bind
converges on the same row instead of leaking a second identity — and never
revives one the run's teardown already revoked."""

import asyncio

import pytest

from orchestrator.activities.crew import CrewActivities
from orchestrator.config import WorkerSettings
from orchestrator.crewpods import service_account_name
from orchestrator.identity import ephemeral_agent_id
from registry.memory import (
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryCrewRequests,
    InMemoryRunParticipants,
    InMemoryRuns,
)

RUN_ID = "run-01CREW"

SPEC = {
    "run_id": RUN_ID,
    "company": "hello-saas",
    "crew_template": "feature-delivery",
    "member": "executor",
    "role_template": "backend-engineer",
    "parent_agent_id": "pm-1",
}


def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
    )


@pytest.fixture
def participants() -> InMemoryRunParticipants:
    return InMemoryRunParticipants()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def crew(settings, participants, audit) -> CrewActivities:
    return CrewActivities(
        settings,
        participants,
        audit,
        InMemoryCrewRequests(),
        InMemoryCompanies(),
        InMemoryBudgetLedger(),
        InMemoryRuns(),
    )


def test_bind_writes_the_whole_identity_row(crew, participants, audit):
    bound = asyncio.run(crew.bind_participant(dict(SPEC)))

    expected_id = ephemeral_agent_id("feature-delivery", "executor", RUN_ID)
    assert bound["agent_id"] == expected_id
    assert bound["service_account"] == service_account_name(expected_id)

    # The row is what the gateways verify against — the whole identity:
    # crew coordinates for the compiled crew rules, plus company, catalog
    # template and namespace.
    row = participants.active(expected_id)
    assert row["run_id"] == RUN_ID
    assert row["parent_agent_id"] == "pm-1"
    assert row["crew_template"] == "feature-delivery"
    assert row["role"] == "executor"  # the member name = crew_role
    assert row["company"] == "hello-saas"
    assert row["role_template"] == "backend-engineer"  # the catalog template
    assert row["namespace"] == "hello-saas"
    assert row["service_account"] == service_account_name(expected_id)

    (event,) = audit.events
    assert event.source == "process-lifecycle"
    assert event.tool == "lifecycle.bind" and event.rule == "participant_bound"
    assert event.agent_id == expected_id and event.run_id == RUN_ID
    assert event.parent_agent_id == "pm-1"


def test_retried_bind_converges_on_one_identity(crew, participants):
    first = asyncio.run(crew.bind_participant(dict(SPEC)))
    again = asyncio.run(crew.bind_participant(dict(SPEC)))

    assert again["agent_id"] == first["agent_id"]
    assert len(participants.rows) == 1
    assert participants.active(first["agent_id"]) is not None


def test_a_bind_that_lost_the_race_with_teardown_is_refused(crew, participants):
    """Revocation is final. A retry that reaches the bind
    after its run's teardown swept the row must not resurrect the identity
    — that is how a crew pod ended up outliving its run, holding a revoked
    identity with nothing left to collect it."""
    from registry import ParticipantRevoked

    first = asyncio.run(crew.bind_participant(dict(SPEC)))
    assert participants.terminate_for_run(RUN_ID) == [first["agent_id"]]

    with pytest.raises(ParticipantRevoked):
        asyncio.run(crew.bind_participant(dict(SPEC)))
    assert participants.active(first["agent_id"]) is None


def test_revoke_sweeps_the_run_and_audits_each(crew, participants, audit):
    asyncio.run(crew.bind_participant(dict(SPEC)))
    asyncio.run(crew.bind_participant({**SPEC, "member": "planner"}))
    audit.events.clear()

    swept = asyncio.run(crew.revoke_participants(RUN_ID))
    assert sorted(swept) == sorted(
        [
            ephemeral_agent_id("feature-delivery", "executor", RUN_ID),
            ephemeral_agent_id("feature-delivery", "planner", RUN_ID),
        ]
    )
    for agent_id in swept:
        assert participants.active(agent_id) is None

    assert [(e.tool, e.rule, e.decision) for e in audit.events] == [
        ("lifecycle.revoke", "participant_revoked", "allow"),
        ("lifecycle.revoke", "participant_revoked", "allow"),
    ]
    assert {e.agent_id for e in audit.events} == set(swept)

    # Idempotent: a second sweep finds nothing and audits nothing.
    audit.events.clear()
    assert asyncio.run(crew.revoke_participants(RUN_ID)) == []
    assert audit.events == []
