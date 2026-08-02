import json

import pytest
from pydantic import ValidationError

from common.audit import AuditEvent


def make_event(**overrides):
    fields = {
        "ts": "2026-07-20T17:00:00+00:00",
        "run_id": "run-01JZXA0000000000000000000A",
        "agent_id": "backend-engineer-1",
        "role": "backend-engineer",
        "tool": "sandbox.read_file",
        "args_digest": "a" * 64,
        "decision": "allow",
        "rule": None,
        "latency_ms": 12,
        "backend_status": "ok",
    }
    fields.update(overrides)
    return AuditEvent(**fields)


def test_round_trip_matches_contract():
    event = make_event(decision="deny", rule="branch_must_be_feature", backend_status=None)
    parsed = json.loads(event.model_dump_json())
    assert parsed == {
        "ts": "2026-07-20T17:00:00+00:00",
        "run_id": "run-01JZXA0000000000000000000A",
        "agent_id": "backend-engineer-1",
        "role": "backend-engineer",
        "tool": "sandbox.read_file",
        "args_digest": "a" * 64,
        "decision": "deny",
        "rule": "branch_must_be_feature",
        "latency_ms": 12,
        "backend_status": None,
    }


def test_decision_and_backend_status_are_constrained():
    with pytest.raises(ValidationError):
        make_event(decision="maybe")
    with pytest.raises(ValidationError):
        make_event(backend_status="crashed")
