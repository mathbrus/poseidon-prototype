"""The deployment's timeout profiles.

Sizing depends on the brain the deployment runs (a paid LLM takes minutes
per step; the mock answers instantly). Workflow code cannot read the
environment under Temporal's sandbox, so the worker hands the profile to
every workflow through its first config activity.
"""

from dataclasses import fields

from orchestrator.workflows.base import (
    BOOTSTRAP_S,
    FAST_TIMEOUTS,
    LISTING_SETTLE_S,
    PROFILES,
    Timeouts,
)


def test_the_two_profiles_are_the_generous_default_and_the_fast_one():
    assert PROFILES == {"default": Timeouts(), "fast": FAST_TIMEOUTS}
    assert Timeouts().dispatch_s == 3600


def test_fast_profile_is_strictly_tighter_on_every_bound():
    default = Timeouts()
    for field in fields(Timeouts):
        fast_value = getattr(FAST_TIMEOUTS, field.name)
        assert fast_value < getattr(default, field.name), field.name
        assert fast_value > 0, field.name


def test_fast_dispatch_still_outlives_a_single_attempt():
    # schedule_to_close must leave room for a retry after a worker kill.
    assert FAST_TIMEOUTS.dispatch_schedule_to_close_s > FAST_TIMEOUTS.dispatch_s
    assert FAST_TIMEOUTS.dispatch_heartbeat_s < FAST_TIMEOUTS.dispatch_s


def test_the_bootstrap_bound_covers_a_registry_round_trip_on_either_profile():
    """The birth activities run before the profile is known; their bound
    must not be tighter than what the fast profile allows a step."""
    assert BOOTSTRAP_S <= FAST_TIMEOUTS.step_s


def test_the_bootstrap_filing_bound_covers_the_listing_settle_on_either_profile():
    """The filing waits up to LISTING_SETTLE_S for its tickets to be listed
    after creating them; its activity bound is a step plus that wait, so a
    slow host can never time the filing out into a second attempt."""
    for profile in PROFILES.values():
        assert profile.step_s + LISTING_SETTLE_S > LISTING_SETTLE_S
        assert profile.step_s > 0
