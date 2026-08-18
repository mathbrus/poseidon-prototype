"""The conduct heartbeat's guardrails, proven at the workflow layer: the
real ConductWorkflow runs against Temporal's time-skipping test environment
with scripted activities — the tick's sequencing, cap halts, refusals and
revocation are exactly what the activity units cannot see.

Each test scripts a queue of per-tick configs ending in None, so the loop
always stops itself through the same revocation path production uses —
every test is therefore also a revocation test."""

import asyncio
import uuid
from dataclasses import dataclass, field
from typing import Any

import pytest
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from orchestrator.workflows.conduct import ConductInput, ConductWorkflow
from orchestrator.workflows.implement import FAST_TIMEOUTS

pytestmark = pytest.mark.anyio

COMPANY = "hello-saas"

CONFIG = {
    "conductor": "pm",
    "heartbeat_minutes": 30,
    "max_concurrent_runs": 1,
    "may_start": ["implement", "review"],
    "daily_cap_usd": 20.0,
}


def observation(**overrides: Any) -> dict[str, Any]:
    observed = {
        "active_runs": 0,
        "daily_spent_usd": 0.0,
        "backlog": [{"number": 5, "title": "Add slugify", "url": "https://i/5"}],
        "reviewable_prs": [],
        "recent_runs": [],
    }
    observed.update(overrides)
    return observed


@dataclass
class Harness:
    """Scripted activities + captured effects for one workflow run. The
    config queue drives the tick count: when it runs dry the loop reads
    None and stops — the production revocation path."""

    configs: list[dict[str, Any] | None] = field(default_factory=list)
    observations: list[dict[str, Any]] = field(default_factory=list)
    decisions: list[dict[str, Any]] = field(default_factory=list)
    deny_rules: list[str | None] = field(default_factory=list)

    decide_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    authorized: list[tuple[str, str, int, float]] = field(default_factory=list)
    ticks: list[tuple[str, str]] = field(default_factory=list)
    gate_events: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def children(self) -> list[tuple[str, dict[str, Any]]]:
        return _CHILDREN

    def activities(self) -> list[Any]:
        harness = self

        @activity.defn(name="conduct_config")
        async def conduct_config() -> dict[str, Any] | None:
            return harness.configs.pop(0) if harness.configs else None

        @activity.defn(name="conduct_observe")
        async def conduct_observe() -> dict[str, Any]:
            return harness.observations.pop(0)

        @activity.defn(name="conduct_decide")
        async def conduct_decide(
            run_id: str, observed: dict[str, Any], may_start: list[str]
        ) -> dict[str, Any]:
            harness.decide_calls.append((run_id, may_start))
            return harness.decisions.pop(0)

        @activity.defn(name="conduct_authorize")
        async def conduct_authorize(
            run_id: str, workflow_name: str, active_runs: int, daily_spent: float
        ) -> None:
            rule = harness.deny_rules.pop(0) if harness.deny_rules else None
            if rule:
                raise ApplicationError(
                    f"conduct.start refused by policy rule {rule!r}",
                    non_retryable=True,
                )
            harness.authorized.append((run_id, workflow_name, active_runs, daily_spent))

        @activity.defn(name="record_conduct_tick")
        async def record_conduct_tick(outcome: str, rule: str, workflow_id: str) -> None:
            harness.ticks.append((outcome, rule))

        @activity.defn(name="record_gate_outcome")
        async def record_gate_outcome(
            run_id: str, gate: str, kind: str, workflow_id: str
        ) -> None:
            harness.gate_events.append((run_id, gate, kind))

        return [
            conduct_config,
            conduct_observe,
            conduct_decide,
            conduct_authorize,
            record_conduct_tick,
            record_gate_outcome,
        ]


# Temporal requires globally-referenceable workflow classes, so the child
# stubs record into a module-level list, cleared per test.
_CHILDREN: list[tuple[str, dict[str, Any]]] = []


@workflow.defn(name="implement", sandboxed=False)
class StubImplement:
    @workflow.run
    async def run(self, input: Any) -> None:
        _CHILDREN.append(("implement", dict(input)))


@workflow.defn(name="review", sandboxed=False)
class StubReview:
    @workflow.run
    async def run(self, input: Any) -> None:
        _CHILDREN.append(("review", dict(input)))


@pytest.fixture(autouse=True)
def _clear_children():
    _CHILDREN.clear()


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


@pytest.fixture
async def env():
    # The time-skipping environment fetches Temporal's test server binary
    # on first use; a machine that cannot skips rather than failing, like
    # the compiler's OPA decision tests.
    try:
        environment = await WorkflowEnvironment.start_time_skipping()
    except Exception as exc:
        pytest.skip(f"temporal test server unavailable: {exc}")
    yield environment
    await environment.shutdown()


async def run_conduct(client: Client, harness: Harness) -> dict[str, Any]:
    """Run the heartbeat to its self-stop and return its final result —
    following the continue-as-new chain across every scripted tick."""
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[ConductWorkflow, StubImplement, StubReview],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await client.start_workflow(
            "conduct",
            ConductInput(company=COMPANY, tick_interval_s=1, timeouts=FAST_TIMEOUTS),
            id=f"conduct-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        return await handle.result()


async def test_started_tick_authorizes_then_spawns_the_chosen_child(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[observation()],
        decisions=[{"action": "implement", "target": 5, "rationale": "oldest work"}],
    )
    result = await run_conduct(env.client, harness)

    (decide_run_id, may_start) = harness.decide_calls[0]
    assert may_start == ["implement", "review"]
    # Policy first, then the child — under the same minted run id.
    assert harness.authorized == [(decide_run_id, "implement", 0, 0.0)]
    (kind, child_input) = harness.children[0]
    assert kind == "implement"
    assert child_input["issue_number"] == 5
    assert child_input["run_id"] == decide_run_id
    assert harness.ticks[0] == ("started", f"implement_5_as_{decide_run_id}")
    # The config queue ran dry: the loop stopped through revocation.
    assert harness.ticks[-1] == ("stopped", "conduct_section_absent")
    assert result == {"stopped": "no conduct section in the applied blueprint"}


async def test_review_decision_starts_a_review_child(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[
            observation(backlog=[], reviewable_prs=[{"number": 4, "url": "u"}])
        ],
        decisions=[{"action": "review", "target": 4, "rationale": "finish first"}],
    )
    await run_conduct(env.client, harness)
    (kind, child_input) = harness.children[0]
    assert kind == "review"
    assert child_input["pr_number"] == 4


async def test_second_eligible_issue_waits_at_the_concurrency_cap(env):
    """max_concurrent_runs: 1 with a run in flight: the backlog has an
    eligible issue, and the tick still starts nothing — audited as
    runs_in_flight, with the PM never even asked."""
    harness = Harness(
        configs=[CONFIG],
        observations=[observation(active_runs=1)],
    )
    await run_conduct(env.client, harness)
    assert harness.decide_calls == []
    assert harness.children == []
    assert harness.ticks[0] == ("idle", "runs_in_flight")


async def test_daily_cap_halts_visibly_and_resumes_when_spend_clears(env):
    """At the cap the tick halts with a first-class process-gate event; a
    later tick with headroom (next day, or a raised cap) starts work again
    with no human restart."""
    harness = Harness(
        configs=[CONFIG, CONFIG],
        observations=[
            observation(daily_spent_usd=20.0),
            observation(daily_spent_usd=0.0),
        ],
        decisions=[{"action": "implement", "target": 5, "rationale": "fresh day"}],
    )
    await run_conduct(env.client, harness)
    assert harness.gate_events == [(f"conduct-{COMPANY}", "conduct_daily_cap", "halted")]
    assert harness.ticks[0] == ("idle", "daily_cap_reached")
    assert harness.ticks[1][0] == "started"
    assert len(harness.children) == 1


async def test_policy_deny_refuses_the_tick_but_not_the_heartbeat(env):
    """A conduct.start deny ends the tick with nothing started — and the
    next tick still happens: refusals are outcomes, not crashes."""
    harness = Harness(
        configs=[CONFIG, CONFIG],
        observations=[observation(), observation()],
        decisions=[
            {"action": "implement", "target": 5, "rationale": "try"},
            {"action": "implement", "target": 5, "rationale": "retry"},
        ],
        deny_rules=["conduct_start_over_daily_cap", None],
    )
    await run_conduct(env.client, harness)
    assert harness.ticks[0][0] == "refused"
    assert "conduct_start_over_daily_cap" in harness.ticks[0][1]
    assert harness.ticks[1][0] == "started"
    assert len(harness.children) == 1  # only the allowed tick started work


async def test_pm_idle_is_a_recorded_outcome(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[observation()],
        decisions=[{"action": "idle", "target": None, "rationale": "not worth it"}],
    )
    await run_conduct(env.client, harness)
    assert harness.children == []
    assert harness.authorized == []
    assert harness.ticks[0] == ("idle", "pm_chose_idle")


async def test_empty_observation_never_wakes_the_pm(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[observation(backlog=[], reviewable_prs=[])],
    )
    await run_conduct(env.client, harness)
    assert harness.decide_calls == []
    assert harness.ticks[0] == ("idle", "backlog_empty")


async def test_revocation_stops_a_running_loop(env):
    """The mid-flight diff: tick one runs under a declared section, the
    section disappears, and the loop's next tick stops it cleanly with the
    stop recorded — no ensure-loop involvement, no kill."""
    harness = Harness(
        configs=[CONFIG, None],
        observations=[observation()],
        decisions=[{"action": "implement", "target": 5, "rationale": "go"}],
    )
    result = await run_conduct(env.client, harness)
    assert harness.ticks[0][0] == "started"
    assert harness.ticks[1] == ("stopped", "conduct_section_absent")
    assert result["stopped"].startswith("no conduct section")


async def test_heartbeat_survives_worker_death_between_ticks(env):
    """Kill the worker after a tick started work; a fresh worker picks the
    parked loop up from Temporal's history and finishes it — with no
    duplicate child from the replay."""
    harness = Harness(
        configs=[CONFIG, None],
        observations=[observation()],
        decisions=[{"action": "implement", "target": 5, "rationale": "go"}],
    )
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"

    def worker() -> Worker:
        return Worker(
            env.client,
            task_queue=task_queue,
            workflows=[ConductWorkflow, StubImplement, StubReview],
            activities=harness.activities(),
            workflow_runner=UnsandboxedWorkflowRunner(),
            # No sticky queue: the dead worker's cached-workflow affinity
            # would otherwise park the post-timer task until a sticky
            # timeout the frozen test clock never reaches. Production
            # pays that timeout in real seconds; the proof here is the
            # handoff itself.
            max_cached_workflows=0,
        )

    async with worker():
        handle = await env.client.start_workflow(
            "conduct",
            ConductInput(company=COMPANY, tick_interval_s=1, timeouts=FAST_TIMEOUTS),
            id=f"conduct-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        while not harness.ticks:  # let the first tick finish, then die
            await asyncio.sleep(0.05)
    assert harness.ticks[0][0] == "started"

    async with worker():  # a fresh worker: replay, resume, self-stop
        result = await handle.result()
    assert result["stopped"].startswith("no conduct section")
    assert [t[0] for t in harness.ticks] == ["started", "stopped"]
    assert len(harness.children) == 1  # the replay started nothing twice
