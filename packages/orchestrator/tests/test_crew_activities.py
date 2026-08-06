"""The 16a slice against doubles: mint binds one deterministic identity per
{crew, member, run} and signs a verifiable §5.1 JWT; revoke sweeps every
live participant; both land process-lifecycle audit events; a retried mint
converges on the same row instead of leaking a second identity — and never
revives one the run's teardown already revoked (plan step 23)."""

import asyncio

import pytest

from common import platformjwt
from orchestrator.activities.crew import CrewActivities
from orchestrator.config import WorkerSettings
from orchestrator.identity import ephemeral_agent_id
from registry.memory import InMemoryAuditStore, InMemoryRunParticipants

RUN_ID = "run-01CREW"
KEY = "unit-test-platform-key"

SPEC = {
    "run_id": RUN_ID,
    "company": "hello-saas",
    "crew_template": "feature-delivery",
    "member": "executor",
    "role_template": "backend-engineer",
    "parent_agent_id": "pm-1",
}


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
        platform_jwt_key=KEY,
        ephemeral_ttl_s=600,
    )


@pytest.fixture
def participants() -> InMemoryRunParticipants:
    return InMemoryRunParticipants()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def crew(settings, participants, audit) -> CrewActivities:
    return CrewActivities(settings, participants, audit)


def test_mint_binds_and_signs_the_51_claims(crew, participants, audit):
    minted = asyncio.run(crew.mint_participant(dict(SPEC)))

    expected_id = ephemeral_agent_id("feature-delivery", "executor", RUN_ID)
    assert minted["agent_id"] == expected_id

    # The row is what the gateways verify against (their half, steps 11/12).
    row = participants.active(expected_id)
    assert row["run_id"] == RUN_ID
    assert row["parent_agent_id"] == "pm-1"
    assert row["crew_template"] == "feature-delivery"
    assert row["role"] == "executor"  # the member name = §5.1 crew_role

    claims = platformjwt.verify(KEY, minted["token"], "poseidon-gateway")
    assert claims["sub"] == expected_id
    assert claims["company"] == "hello-saas"
    assert claims["role"] == "backend-engineer"  # the catalog template
    assert claims["lifecycle"] == "ephemeral"
    assert claims["run_id"] == RUN_ID
    assert claims["parent_agent_id"] == "pm-1"

    (event,) = audit.events
    assert event.source == "process-lifecycle"
    assert event.tool == "lifecycle.mint" and event.rule == "participant_minted"
    assert event.agent_id == expected_id and event.run_id == RUN_ID
    assert event.parent_agent_id == "pm-1"
    # The token must never reach the audit store.
    assert minted["token"] not in event.args_digest


def test_retried_mint_converges_on_one_identity(crew, participants):
    first = asyncio.run(crew.mint_participant(dict(SPEC)))
    again = asyncio.run(crew.mint_participant(dict(SPEC)))

    assert again["agent_id"] == first["agent_id"]
    assert len(participants.rows) == 1
    assert participants.active(first["agent_id"]) is not None


def test_a_mint_that_lost_the_race_with_teardown_is_refused(crew, participants):
    """Plan step 23: revocation is final. A retry that reaches the mint
    after its run's teardown swept the row must not resurrect the identity
    — that is how a crew pod ended up outliving its run, holding a revoked
    credential with nothing left to collect it."""
    from registry import ParticipantRevoked

    first = asyncio.run(crew.mint_participant(dict(SPEC)))
    assert participants.terminate_for_run(RUN_ID) == [first["agent_id"]]

    with pytest.raises(ParticipantRevoked):
        asyncio.run(crew.mint_participant(dict(SPEC)))
    assert participants.active(first["agent_id"]) is None


def test_revoke_sweeps_the_run_and_audits_each(crew, participants, audit):
    asyncio.run(crew.mint_participant(dict(SPEC)))
    asyncio.run(crew.mint_participant({**SPEC, "member": "planner"}))
    audit.events.clear()

    swept = asyncio.run(crew.revoke_participants(RUN_ID))
    assert sorted(swept) == sorted([
        ephemeral_agent_id("feature-delivery", "executor", RUN_ID),
        ephemeral_agent_id("feature-delivery", "planner", RUN_ID),
    ])
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


def test_mint_without_the_key_fails_closed(settings, participants, audit):
    keyless = CrewActivities(
        settings.model_copy(update={"platform_jwt_key": ""}), participants, audit
    )
    with pytest.raises(RuntimeError, match="PLATFORM_JWT_KEY"):
        asyncio.run(keyless.mint_participant(dict(SPEC)))
    assert participants.rows == {}
