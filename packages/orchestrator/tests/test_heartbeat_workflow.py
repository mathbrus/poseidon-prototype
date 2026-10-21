"""The heartbeat's guardrails, proven at the workflow layer: the
real HeartbeatWorkflow runs against Temporal's time-skipping test environment
with scripted activities — the tick's sequencing, cap halts, refusals,
revocation and the wake path are exactly what the activity units cannot see.

Each test scripts a queue of per-tick configs ending in None, so the loop
always stops itself through the same path a deleted company takes in
production — every test is therefore also a teardown test. And because the
time-skipping clock only advances while a result is awaited, every test
that chains ticks without sending a wake is also a reconcile-backstop
test: the full wait between those ticks ended by timer, not by signal."""

import asyncio
import uuid
from dataclasses import asdict, dataclass, field
from typing import Any

import pytest
from conftest import heartbeat_spec
from temporalio import activity, workflow
from temporalio.client import Client
from temporalio.exceptions import ApplicationError
from temporalio.testing import WorkflowEnvironment
from temporalio.worker import UnsandboxedWorkflowRunner, Worker

from common.workflows import GATE_HALTED
from orchestrator.workflows.base import FAST_TIMEOUTS
from orchestrator.workflows.heartbeat import HeartbeatInput, HeartbeatWorkflow

pytestmark = pytest.mark.anyio

COMPANY = "hello-saas"

CONFIG = heartbeat_spec(may_start=["implement", "review"])
SCOPING = heartbeat_spec(may_start=["implement", "review", "scope"])


def observation(**overrides: Any) -> dict[str, Any]:
    observed = {
        "active_runs": 0,
        "daily_spent_usd": 0.0,
        "open_tickets": 1,
        "bootstrap": {"pending": [], "exhausted": []},
        "backlog": [
            {"number": 5, "title": "Add slugify", "url": "https://i/5", "attempts": 0}
        ],
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

    observed_caps: list[int] = field(default_factory=list)
    bootstrapped: list[list[str]] = field(default_factory=list)
    # When set, every bootstrap filing raises: the not-idempotent activity
    # the workflow must never retry.
    bootstrap_fails: bool = False
    decide_calls: list[tuple[str, list[str]]] = field(default_factory=list)
    authorized: list[tuple[str, str, int, float, int, int]] = field(default_factory=list)
    ticks: list[tuple[str, str]] = field(default_factory=list)
    gate_events: list[tuple[str, str, str]] = field(default_factory=list)

    @property
    def children(self) -> list[tuple[str, dict[str, Any]]]:
        return _CHILDREN

    def activities(self) -> list[Any]:
        harness = self

        @activity.defn(name="heartbeat_config")
        async def heartbeat_config(company: str) -> dict[str, Any] | None:
            config = harness.configs.pop(0) if harness.configs else None
            if config is None:
                return None
            return {**config, "timeouts": asdict(FAST_TIMEOUTS)}

        @activity.defn(name="heartbeat_observe")
        async def heartbeat_observe(max_attempts: int) -> dict[str, Any]:
            harness.observed_caps.append(max_attempts)
            return harness.observations.pop(0)

        @activity.defn(name="heartbeat_bootstrap")
        async def heartbeat_bootstrap(names: list[str]) -> list[str]:
            harness.bootstrapped.append(names)
            if harness.bootstrap_fails:
                raise RuntimeError("git host unreachable")
            return list(names)

        @activity.defn(name="heartbeat_decide")
        async def heartbeat_decide(
            run_id: str,
            observed: dict[str, Any],
            may_start: list[str],
            max_open_tickets: int,
        ) -> dict[str, Any]:
            harness.decide_calls.append((run_id, may_start))
            return harness.decisions.pop(0)

        @activity.defn(name="heartbeat_authorize")
        async def heartbeat_authorize(
            run_id: str,
            workflow_name: str,
            active_runs: int,
            daily_spent: float,
            attempts: int,
            open_tickets: int,
        ) -> None:
            rule = harness.deny_rules.pop(0) if harness.deny_rules else None
            if rule:
                raise ApplicationError(
                    f"heartbeat.start refused by policy rule {rule!r}",
                    non_retryable=True,
                )
            harness.authorized.append(
                (run_id, workflow_name, active_runs, daily_spent, attempts, open_tickets)
            )

        @activity.defn(name="record_tick")
        async def record_tick(outcome: str, rule: str, workflow_id: str) -> None:
            harness.ticks.append((outcome, rule))

        @activity.defn(name="record_gate_outcome")
        async def record_gate_outcome(
            run_id: str, gate: str, kind: str, workflow_id: str
        ) -> None:
            harness.gate_events.append((run_id, gate, kind))

        return [
            heartbeat_config,
            heartbeat_observe,
            heartbeat_bootstrap,
            heartbeat_decide,
            heartbeat_authorize,
            record_tick,
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


@workflow.defn(name="scope", sandboxed=False)
class StubScope:
    @workflow.run
    async def run(self, input: Any) -> None:
        _CHILDREN.append(("scope", dict(input)))


@pytest.fixture(autouse=True)
def _clear_children():
    _CHILDREN.clear()


def anyio_backend() -> str:
    return "asyncio"


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


async def run_heartbeat(client: Client, harness: Harness) -> dict[str, Any]:
    """Run the heartbeat to its self-stop and return its final result —
    following the continue-as-new chain across every scripted tick."""
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        client,
        task_queue=task_queue,
        workflows=[HeartbeatWorkflow, StubImplement, StubReview, StubScope],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await client.start_workflow(
            "heartbeat",
            HeartbeatInput(company=COMPANY),
            id=f"heartbeat-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        return await handle.result()


async def test_started_tick_authorizes_then_spawns_the_chosen_child(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[observation()],
        decisions=[{"action": "implement", "target": 5, "rationale": "oldest work"}],
    )
    result = await run_heartbeat(env.client, harness)

    (decide_run_id, may_start) = harness.decide_calls[0]
    assert may_start == ["implement", "review"]
    # Policy first, then the child — under the same minted run id, with
    # the blueprint's attempt cap having reached the observation.
    assert harness.observed_caps == [CONFIG["max_attempts"]]
    assert harness.authorized == [(decide_run_id, "implement", 0, 0.0, 0, 1)]
    (kind, child_input) = harness.children[0]
    assert kind == "implement"
    assert child_input["issue_number"] == 5
    assert child_input["run_id"] == decide_run_id
    assert harness.ticks[0] == ("started", f"implement_5_as_{decide_run_id}")
    # The config queue ran dry: the loop stopped as a deleted company's does.
    assert harness.ticks[-1] == ("stopped", "blueprint_absent")
    assert result == {"stopped": "no applied blueprint for the company"}


async def test_review_decision_starts_a_review_child(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[
            observation(
                backlog=[],
                reviewable_prs=[{"number": 4, "url": "u", "attempts": 1}],
            )
        ],
        decisions=[{"action": "review", "target": 4, "rationale": "finish first"}],
    )
    await run_heartbeat(env.client, harness)
    (kind, child_input) = harness.children[0]
    assert kind == "review"
    assert child_input["pr_number"] == 4
    # The chosen PR's prior attempt rode the policy input: a second review
    # of a PR whose first aborted is bounded by the compiled decision.
    assert harness.authorized[0][1:] == ("review", 0, 0.0, 1, 1)


async def test_scope_decision_starts_a_scope_child(env):
    """Scoping the next slice is a run like any other: authorized with the
    open-ticket count on the input, started under the minted id, no item
    and no attempts."""
    harness = Harness(
        configs=[SCOPING],
        observations=[observation(backlog=[], open_tickets=0)],
        decisions=[{"action": "scope", "target": None, "rationale": "backlog dry"}],
    )
    await run_heartbeat(env.client, harness)
    (decide_run_id, may_start) = harness.decide_calls[0]
    assert may_start == ["implement", "review", "scope"]
    assert harness.authorized == [(decide_run_id, "scope", 0, 0.0, 0, 0)]
    (kind, child_input) = harness.children[0]
    assert kind == "scope"
    assert child_input == {"run_id": decide_run_id}
    assert harness.ticks[0] == ("started", f"scope_as_{decide_run_id}")


async def test_an_empty_backlog_still_asks_the_pm_when_scoping_is_open(env):
    """With nothing to implement or review, the PM is still asked: the
    next slice may be worth scoping, or not — idle is its call."""
    harness = Harness(
        configs=[SCOPING],
        observations=[observation(backlog=[], reviewable_prs=[], open_tickets=0)],
        decisions=[{"action": "idle", "target": None, "rationale": "strategy done"}],
    )
    await run_heartbeat(env.client, harness)
    assert len(harness.decide_calls) == 1
    assert harness.ticks[0] == ("idle", "pm_chose_idle")


async def test_an_empty_backlog_at_the_ticket_cap_never_wakes_the_pm(env):
    """Scoping is closed at `max_open_tickets`, so an empty startable set
    is an empty world: the PM is not asked."""
    harness = Harness(
        configs=[SCOPING],
        observations=[observation(backlog=[], reviewable_prs=[], open_tickets=5)],
    )
    await run_heartbeat(env.client, harness)
    assert harness.decide_calls == []
    assert harness.ticks[0] == ("idle", "backlog_empty")


async def test_an_absent_document_is_bootstrapped_before_anything_else(env):
    """A vision-only company: the tick files the bootstrap tickets for the
    absent documents, asks the PM nothing, and wakes itself so the next
    tick offers them."""
    harness = Harness(
        configs=[CONFIG, CONFIG, None],
        observations=[
            observation(
                backlog=[],
                bootstrap={"pending": ["strategy", "architecture"], "exhausted": []},
            ),
            observation(backlog=[], reviewable_prs=[]),
        ],
    )
    await run_heartbeat(env.client, harness)
    assert harness.bootstrapped == [["strategy", "architecture"]]
    assert harness.decide_calls == []
    assert harness.ticks[0] == ("idle", "bootstrap_filed:strategy,architecture")
    # The second tick ran under the frozen clock: the tick woke itself.
    assert harness.ticks[1][0] == "idle"


async def test_a_failed_bootstrap_filing_is_never_retried(env):
    """Filing is not idempotent: a filing that fails is attempted once,
    the tick is refused by name, and the next tick reads the world for
    itself — under a retrying policy the harness would count three."""
    harness = Harness(
        configs=[CONFIG],
        observations=[
            observation(backlog=[], bootstrap={"pending": ["strategy"], "exhausted": []})
        ],
        bootstrap_fails=True,
    )
    await run_heartbeat(env.client, harness)
    assert harness.bootstrapped == [["strategy"]]
    assert harness.ticks[0][0] == "refused"
    assert "git host unreachable" in harness.ticks[0][1]


async def test_an_exhausted_bootstrap_halts_by_name(env):
    """Every attempt on the bootstrap ticket aborted: the loop refuses to
    scope forever and records a process-gate halt instead."""
    harness = Harness(
        configs=[CONFIG],
        observations=[
            observation(backlog=[], bootstrap={"pending": [], "exhausted": ["strategy"]})
        ],
    )
    await run_heartbeat(env.client, harness)
    assert harness.decide_calls == []
    assert harness.ticks[0] == ("idle", "bootstrap_exhausted:strategy")
    assert harness.gate_events == [
        (f"heartbeat-{COMPANY}", "heartbeat_bootstrap_exhausted", GATE_HALTED)
    ]


async def test_second_eligible_issue_waits_at_the_concurrency_cap(env):
    """max_concurrent_runs: 1 with a run in flight: the backlog has an
    eligible issue, and the tick still starts nothing — audited as
    runs_in_flight, with the PM never even asked."""
    harness = Harness(
        configs=[CONFIG],
        observations=[observation(active_runs=1)],
    )
    await run_heartbeat(env.client, harness)
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
    await run_heartbeat(env.client, harness)
    assert harness.gate_events == [
        (f"heartbeat-{COMPANY}", "heartbeat_daily_cap", GATE_HALTED)
    ]
    assert harness.ticks[0] == ("idle", "daily_cap_reached")
    assert harness.ticks[1][0] == "started"
    assert len(harness.children) == 1


async def test_policy_deny_refuses_the_tick_but_not_the_heartbeat(env):
    """A heartbeat.start deny ends the tick with nothing started — and the
    next tick still happens: refusals are outcomes, not crashes."""
    harness = Harness(
        configs=[CONFIG, CONFIG],
        observations=[observation(), observation()],
        decisions=[
            {"action": "implement", "target": 5, "rationale": "try"},
            {"action": "implement", "target": 5, "rationale": "retry"},
        ],
        deny_rules=["heartbeat_start_over_daily_cap", None],
    )
    await run_heartbeat(env.client, harness)
    assert harness.ticks[0][0] == "refused"
    assert "heartbeat_start_over_daily_cap" in harness.ticks[0][1]
    assert harness.ticks[1][0] == "started"
    assert len(harness.children) == 1  # only the allowed tick started work


async def test_pm_idle_is_a_recorded_outcome(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[observation()],
        decisions=[{"action": "idle", "target": None, "rationale": "not worth it"}],
    )
    await run_heartbeat(env.client, harness)
    assert harness.children == []
    assert harness.authorized == []
    assert harness.ticks[0] == ("idle", "pm_chose_idle")


async def test_empty_observation_never_wakes_the_pm(env):
    harness = Harness(
        configs=[CONFIG],
        observations=[observation(backlog=[], reviewable_prs=[])],
    )
    await run_heartbeat(env.client, harness)
    assert harness.decide_calls == []
    assert harness.ticks[0] == ("idle", "backlog_empty")


async def test_deleting_the_company_stops_a_running_loop(env):
    """The mid-flight teardown: tick one runs under an applied company,
    the company disappears, and the loop's next tick stops it cleanly
    with the stop recorded — no ensure-loop involvement, no kill."""
    harness = Harness(
        configs=[CONFIG, None],
        observations=[observation()],
        decisions=[{"action": "implement", "target": 5, "rationale": "go"}],
    )
    result = await run_heartbeat(env.client, harness)
    assert harness.ticks[0][0] == "started"
    assert harness.ticks[1] == ("stopped", "blueprint_absent")
    assert result["stopped"].startswith("no applied blueprint")


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
            workflows=[HeartbeatWorkflow, StubImplement, StubReview, StubScope],
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
            "heartbeat",
            HeartbeatInput(company=COMPANY),
            id=f"heartbeat-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        while not harness.ticks:  # let the first tick finish, then die
            await asyncio.sleep(0.05)
    assert harness.ticks[0][0] == "started"

    async with worker():  # a fresh worker: replay, resume, self-stop
        result = await handle.result()
    assert result["stopped"].startswith("no applied blueprint")
    assert [t[0] for t in harness.ticks] == ["started", "stopped"]
    assert len(harness.children) == 1  # the replay started nothing twice


def _quiet(n: int) -> list[dict[str, Any]]:
    return [observation(backlog=[], reviewable_prs=[]) for _ in range(n)]


PAUSED = {**CONFIG, "paused": {"by": "owner", "at": "2026-10-20T12:00:00+00:00"}}


async def test_a_paused_tick_looks_at_nothing_and_a_resumed_one_starts_work(env):
    """While a human holds the loop a tick records the pause with their
    name and neither observes, decides nor starts; the first tick after
    the pause is lifted works like any other, with no human restart."""
    harness = Harness(
        configs=[PAUSED, PAUSED, CONFIG],
        observations=[observation()],
        decisions=[{"action": "implement", "target": 5, "rationale": "resumed"}],
    )
    await run_heartbeat(env.client, harness)
    assert harness.ticks[:2] == [("paused", "owner"), ("paused", "owner")]
    assert harness.ticks[2][0] == "started"
    # One observation, one decision: the paused ticks asked for neither.
    assert harness.observed_caps == [CONFIG["max_attempts"]]
    assert len(harness.decide_calls) == 1
    assert len(harness.children) == 1


async def test_a_paused_loop_says_so_and_a_wake_leaves_it_paused(env):
    """`heartbeat show` reads `paused` from the state query, and a wake
    (a finished run, a human's nudge) ticks once more without starting
    anything — the pause is the registry's fact, not the signal's."""
    harness = Harness(configs=[PAUSED, PAUSED, None])
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[HeartbeatWorkflow, StubImplement, StubReview, StubScope],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            "heartbeat",
            HeartbeatInput(company=COMPANY),
            id=f"heartbeat-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        while await handle.query("state") != "paused":
            await asyncio.sleep(0.05)
        await handle.signal("wake")
        while len(harness.ticks) < 2:
            await asyncio.sleep(0.05)
        await handle.result()
    assert harness.ticks == [
        ("paused", "owner"),
        ("paused", "owner"),
        ("stopped", "blueprint_absent"),
    ]
    assert harness.children == []


async def test_wake_cuts_the_sleep_short(env):
    """While no result is awaited the test clock is frozen, so the
    reconcile timer cannot fire: the second tick below can only have been
    caused by the wake signal."""
    harness = Harness(configs=[CONFIG, CONFIG, None], observations=_quiet(2))
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[HeartbeatWorkflow, StubImplement, StubReview, StubScope],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            "heartbeat",
            HeartbeatInput(company=COMPANY),
            id=f"heartbeat-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        while await handle.query("state") != "sleeping":
            await asyncio.sleep(0.05)
        await handle.signal("wake")
        while len(harness.ticks) < 2:
            await asyncio.sleep(0.05)
        result = await handle.result()
    assert [t[0] for t in harness.ticks] == ["idle", "idle", "stopped"]
    assert result["stopped"].startswith("no applied blueprint")


async def test_wakes_mid_tick_coalesce_to_one_extra_tick(env):
    """Two wakes land while a tick is still observing: the sleep is
    skipped once and exactly one extra tick follows — a flag, not a
    queue, so wakes can never pile into a storm."""
    harness = Harness(configs=[CONFIG, CONFIG, None], observations=_quiet(2))
    observing = asyncio.Event()
    release = asyncio.Event()

    @activity.defn(name="heartbeat_observe")
    async def blocking_observe(max_attempts: int) -> dict[str, Any]:
        if not release.is_set():
            observing.set()
            await release.wait()
        harness.observed_caps.append(max_attempts)
        return harness.observations.pop(0)

    activities = harness.activities()
    activities[1] = blocking_observe
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[HeartbeatWorkflow, StubImplement, StubReview, StubScope],
        activities=activities,
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            "heartbeat",
            HeartbeatInput(company=COMPANY),
            id=f"heartbeat-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        await observing.wait()
        await handle.signal("wake")
        await handle.signal("wake")
        release.set()
        while len(harness.ticks) < 2:
            await asyncio.sleep(0.05)
        # The frozen clock rules the timer out and no further wake was
        # sent: a third tick now would be the coalescing gone wrong.
        await asyncio.sleep(0.5)
        assert len(harness.ticks) == 2
        assert await handle.query("state") == "sleeping"
        await handle.result()
    assert [t[0] for t in harness.ticks] == ["idle", "idle", "stopped"]


async def test_a_pending_wake_skips_the_first_sleep_then_the_loop_rests(env):
    """A wake that raced the previous run's close rides the input: the
    first sleep is skipped, and — the regression this pins — the flag is
    consumed, so the loop settles back onto its wait instead of spinning."""
    harness = Harness(configs=[CONFIG, CONFIG, None], observations=_quiet(2))
    task_queue = f"tq-{uuid.uuid4().hex[:8]}"
    async with Worker(
        env.client,
        task_queue=task_queue,
        workflows=[HeartbeatWorkflow, StubImplement, StubReview, StubScope],
        activities=harness.activities(),
        workflow_runner=UnsandboxedWorkflowRunner(),
    ):
        handle = await env.client.start_workflow(
            "heartbeat",
            HeartbeatInput(company=COMPANY, pending_wake=True),
            id=f"heartbeat-{COMPANY}-{uuid.uuid4().hex[:8]}",
            task_queue=task_queue,
        )
        # Both ticks land under the frozen clock: the input alone cut the
        # first sleep short.
        while len(harness.ticks) < 2:
            await asyncio.sleep(0.05)
        assert await handle.query("state") == "sleeping"
        result = await handle.result()
    assert [t[0] for t in harness.ticks] == ["idle", "idle", "stopped"]
    assert result["stopped"].startswith("no applied blueprint")
