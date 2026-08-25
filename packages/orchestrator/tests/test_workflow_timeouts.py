"""The timeout profiles carried in the workflow input.

Sizing belongs to the client because it depends on the brain behind the run
(a paid LLM takes minutes per step; the mock answers instantly), and
workflow code cannot read the environment under Temporal's sandbox.
"""

from dataclasses import fields

from orchestrator.workflows.base import FAST_TIMEOUTS, Timeouts
from orchestrator.workflows.implement import ImplementInput


def test_default_input_keeps_the_generous_real_brain_bounds():
    input = ImplementInput(run_id="run-1", task_text="task")
    assert input.timeouts is None  # the workflow falls back to Timeouts()
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
