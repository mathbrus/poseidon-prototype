import json

import pytest
from pydantic import ValidationError

from common.audit import RATIONALE_LIMIT, AuditEvent


def make_event(**overrides):
    fields = {
        "ts": "2026-07-20T17:00:00+00:00",
        "run_id": "run-01JZXA0000000000000000000A",
        "agent_id": "implementer-1",
        "role": "implementer",
        "tool": "sandbox.read_file",
        "args_digest": "a" * 64,
        "decision": "allow",
        "rule": None,
        "latency_ms": 12,
        "backend_status": "ok",
        "source": "gateway",
    }
    fields.update(overrides)
    return AuditEvent(**fields)


def test_round_trip_matches_contract():
    """The audit event shape: decision fields plus source and the
    attribution columns, empty when genuinely absent."""
    event = make_event(
        decision="deny", rule="branch_must_be_feature", backend_status=None
    )
    parsed = json.loads(event.model_dump_json())
    assert parsed == {
        "ts": "2026-07-20T17:00:00+00:00",
        "run_id": "run-01JZXA0000000000000000000A",
        "agent_id": "implementer-1",
        "role": "implementer",
        "tool": "sandbox.read_file",
        "args_digest": "a" * 64,
        "decision": "deny",
        "rule": "branch_must_be_feature",
        "rationale": None,
        "latency_ms": 12,
        "backend_status": None,
        "source": "gateway",
        "company": "",
        "lifecycle": "",
        "parent_agent_id": "",
        "workflow": "",
    }


def test_attribution_fields_round_trip():
    event = make_event(
        source="sandbox-infra",
        company="hello-saas",
        lifecycle="ephemeral",
        parent_agent_id="pm-1",
        workflow="implement",
    )
    assert event.source == "sandbox-infra"
    assert event.company == "hello-saas"
    assert event.parent_agent_id == "pm-1"


def test_a_rationale_is_bounded():
    """A stated reason stays a reason: the model refuses one long enough to
    be a transcript, and a row that decided nothing carries none."""
    assert make_event().rationale is None
    stated = make_event(rationale="x" * RATIONALE_LIMIT)
    assert stated.rationale == "x" * RATIONALE_LIMIT
    with pytest.raises(ValidationError):
        make_event(rationale="x" * (RATIONALE_LIMIT + 1))


def test_decision_and_backend_status_are_constrained():
    with pytest.raises(ValidationError):
        make_event(decision="maybe")
    with pytest.raises(ValidationError):
        make_event(backend_status="crashed")
    # Source is a closed vocabulary — a typo'd producer fails loudly, not
    # as a silently unqueryable source.
    with pytest.raises(ValidationError):
        make_event(source="side-channel")
