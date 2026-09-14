import json
import re

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


def test_platform_event_names_are_spelled_once():
    """The registry is closed: one row per name, every source a real one,
    every dotted name the module or the policy contract defines listed —
    except the crew request, which is a decision the platform evaluates and
    records under `lifecycle.record_request`, never a row's own name."""
    from common import audit, policy
    from common.audit import PLATFORM_EVENTS, RATIONALE_EVENTS, SOURCES, gate_event
    from common.workflows import ATTEMPTS_GATE, BOOTSTRAP_GATE, DAILY_CAP_GATE, GATES

    names = [event.name for event in PLATFORM_EVENTS]
    assert len(names) == len(set(names))
    assert {event.source for event in PLATFORM_EVENTS} <= set(SOURCES)
    assert "gateway" not in {event.source for event in PLATFORM_EVENTS}

    registered = set(names)
    dotted = {
        value
        for module in (audit, policy)
        for key, value in vars(module).items()
        if key.isupper()
        and isinstance(value, str)
        and re.fullmatch(r"[a-z]+\.[a-z_]+", value)
    }
    assert dotted - registered == {policy.CREW_REQUEST}
    for gate in (*GATES, ATTEMPTS_GATE, BOOTSTRAP_GATE, DAILY_CAP_GATE):
        assert gate_event(gate) in registered
    assert RATIONALE_EVENTS == {
        audit.HEARTBEAT_DECIDE,
        audit.REVIEW_VERDICT,
        policy.GATE_RESOLVE,
        policy.RUN_CANCEL,
        policy.COMPANY_DELETE,
    }
