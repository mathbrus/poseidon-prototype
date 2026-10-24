"""The vocabulary two components must agree on: what a tick and a gate
record about themselves, spelled once and validated at the writer."""

import pytest

from common.workflows import (
    GATE_APPROVED,
    GATE_AUTOMATED,
    GATE_OUTCOMES,
    GATE_RELEASING,
    TICK_OUTCOMES,
    gate_rule,
    parse_tick_rule,
    tick_rule,
)


def test_gate_rule_spells_every_outcome_once():
    assert [gate_rule(kind) for kind in GATE_OUTCOMES] == [
        f"gate_{kind}" for kind in GATE_OUTCOMES
    ]
    assert len(set(GATE_OUTCOMES)) == len(GATE_OUTCOMES)


def test_gate_rule_refuses_an_unknown_outcome():
    """`timed_out` is not a gate outcome (an expired park is `expired`),
    and nothing can write it."""
    for kind in ("timed_out", "waiting", ""):
        with pytest.raises(ValueError, match="not a gate outcome"):
            gate_rule(kind)


def test_only_a_human_approval_or_automation_releases():
    assert GATE_RELEASING == (GATE_APPROVED, GATE_AUTOMATED)
    assert set(GATE_RELEASING) < set(GATE_OUTCOMES)


def test_tick_rule_round_trips_and_refuses_an_unknown_outcome():
    for outcome in TICK_OUTCOMES:
        assert parse_tick_rule(tick_rule(outcome, "why")) == (outcome, "why")
    with pytest.raises(ValueError, match="not a tick outcome"):
        tick_rule("halted", "")
