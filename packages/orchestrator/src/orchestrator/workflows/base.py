"""What the workflows share: the activity bounds, the retry policies,
deterministic workflow naming, and — for the workflow classes — the run's
birth, crew fulfilment, the budget park, the gate audit trail and
teardown.
"""

from dataclasses import dataclass
from datetime import timedelta
from typing import Any

from temporalio import workflow
from temporalio.common import RetryPolicy
from temporalio.exceptions import ActivityError

from common.workflows import BUDGET_GATE, GATE_APPROVED, GATE_ESCALATED, PARKED_STATE
from orchestrator.gates import Gate

# The marker of the LLM gateway's refusal at the cap: a dispatch whose error carries it
# ran out of budget, and the workflow parks instead of retrying.
BUDGET_MARKER = "budget_exhausted"

STEP_RETRY = RetryPolicy(maximum_attempts=3, backoff_coefficient=2.0)
# For an activity whose effect is not idempotent and whose caller
# re-observes the world anyway: a filing, a nudge. A failure is the
# caller's to notice on its next look, never a second attempt's to repeat.
SINGLE_ATTEMPT = RetryPolicy(maximum_attempts=1)
# The agent works for as long as one attempt allows; heartbeats (one per
# poll) detect a dead worker fast, and the unbounded retry re-attaches to
# the running agent (dispatch answers 409 → poll). schedule_to_close caps
# the whole thing.
DISPATCH_RETRY = RetryPolicy(initial_interval=timedelta(seconds=5), maximum_attempts=0)
TEARDOWN_RETRY = RetryPolicy(maximum_attempts=5, backoff_coefficient=2.0)


def gate_deadline(minutes: int) -> dict[str, timedelta]:
    """A gate's timer from its declared wait: expiry at the deadline,
    escalation halfway — the shape `Gate.wait` takes."""
    return {
        "timeout": timedelta(minutes=minutes),
        "escalate_after": timedelta(seconds=minutes * 30),
    }


@dataclass
class Timeouts:
    """Per-activity timeouts — the deployment's profile.

    Steps are bounded per activity, not per workflow: a workflow-level
    execution timeout would hard-kill without running compensation.

    The sizing depends on the *brain* the deployment runs: a real LLM
    legitimately takes minutes per step, while the deterministic mock
    answers in milliseconds — and against the mock the generous timeouts turn
    a fail-fast bug into a quarter hour of waiting on retries. So the
    profile is a worker setting (`TIMEOUT_PROFILE`), and every workflow
    learns it from its first config activity: workflow code cannot read the
    environment under Temporal's sandbox, but an activity result is
    deterministic on replay.
    """

    step_s: int = 300
    dispatch_s: int = 3600
    dispatch_schedule_to_close_s: int = 7200
    dispatch_heartbeat_s: int = 60
    # Crew fulfilment: identity binding plus one pod per agent, each
    # waited to readiness — slower than a registry step, far shorter than
    # a model loop.
    provision_s: int = 900
    verify_s: int = 900


# Sized for the deterministic mock brain (packages/mockllm): every bound is
# far above what the mock needs, and far below the patience a paid brain
# deserves — so a wedged activity surfaces in seconds, not minutes.
FAST_TIMEOUTS = Timeouts(
    step_s=60,
    dispatch_s=300,
    dispatch_schedule_to_close_s=600,
    dispatch_heartbeat_s=30,
    provision_s=300,
    verify_s=120,
)

# The two profiles a deployment may declare, by the `TIMEOUT_PROFILE` value.
PROFILES = {"default": Timeouts(), "fast": FAST_TIMEOUTS}

# The bound for the registry round trips a workflow makes before it has
# read its profile (the run's birth and config) or after a failed birth
# (teardown's bookkeeping). A ceiling, never a wait: these activities
# answer in milliseconds on either profile.
BOOTSTRAP_S = 30


class RunWorkflow:
    """The base of the run-shaped workflow classes: the run's birth
    (row, budget headroom, shape from the applied blueprint), crew
    fulfilment, the API-visible phase, gate transitions recorded as
    `process-gate` audit events, the budget-park loop around an agent
    dispatch, and teardown.

    Subclasses name their `WORKFLOW`, set `_state` and `_budget_gate`, and
    declare their own signals and the `state` query — Temporal reads those
    off the concrete class.
    """

    WORKFLOW: str
    # A workflow that dispatches the conductor directly and has no node
    # in the blueprint's `workflows` says so; every other one is refused
    # without its node.
    CREWLESS: bool = False
    _state: str
    _budget_gate: Gate
    # The deployment's profile, learned from `workflow_config`; until then
    # (and on a birth that failed before it) the default sizing applies.
    _profile: Timeouts = Timeouts()
    # The run's shape, as `workflow_config` returned it.
    _config: dict[str, Any] = {}

    async def start_run(self, run_id: str, task: str) -> dict[str, Any]:
        """The run's birth: its registry row, the budget-headroom check
        (a run is born with headroom or not at all — enforcement itself
        stays local at the LLM gateway), and the workflow's shape read from
        the applied blueprint, returned as `workflow_config` describes it.
        The shape carries the deployment's timeout profile, which bounds
        every activity from here on."""
        bootstrap = timedelta(seconds=BOOTSTRAP_S)
        await workflow.execute_activity(
            "create_run",
            args=[run_id, self.WORKFLOW, task],
            start_to_close_timeout=bootstrap,
            retry_policy=STEP_RETRY,
        )
        await workflow.execute_activity(
            "check_budget",
            args=[run_id],
            start_to_close_timeout=bootstrap,
            retry_policy=STEP_RETRY,
        )
        config = await workflow.execute_activity(
            "workflow_config",
            args=[self.WORKFLOW, self.CREWLESS],
            start_to_close_timeout=bootstrap,
            retry_policy=STEP_RETRY,
        )
        self._config = config
        self._profile = Timeouts(**config["timeouts"])
        return config

    async def fulfill(
        self,
        run_id: str,
        config: dict[str, Any],
        crew: str | None = None,
    ) -> dict[str, Any]:
        """Stand the run's ephemeral crew up under the conductor's
        authority — fulfilment wins the compiled `crew.request` decision as
        the conductor first — and return the one agent it dispatches, as
        `{agent_id, url}`. `crew` overrides the workflow's configured crew
        (a ticket naming its own). Fulfilment returns the crew's agents by
        role and refuses a crew that declares more than one, so the
        unpacking cannot fail here."""
        fulfilled = await workflow.execute_activity(
            "fulfill_crew",
            args=[
                {
                    "run_id": run_id,
                    "company": config["company"],
                    "crew_template": crew or config["crew"],
                    "conductor": config["conductor"],
                    "workflow": self.WORKFLOW,
                }
            ],
            start_to_close_timeout=timedelta(seconds=self._profile.provision_s),
            heartbeat_timeout=timedelta(seconds=self._profile.dispatch_heartbeat_s),
            retry_policy=STEP_RETRY,
        )
        (agent,) = fulfilled["agents"].values()
        return agent

    async def dispatch_documents(self, run_id: str) -> dict[str, str]:
        """The company's documents as a dispatch carries them — name to
        text for the ones present on `base_ref`, read under the run's id
        for the dispatches that judge work against them."""
        return await workflow.execute_activity(
            "dispatch_documents",
            args=[run_id],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=STEP_RETRY,
        )

    async def teardown(
        self,
        run_id: str,
        handle: dict[str, Any] | None,
        result: dict[str, Any] | None,
        error: str | None,
    ) -> dict[str, Any]:
        """Compensation and finalization on every terminal path: the run's
        ephemeral identities and pods, its sandbox (when one was
        provisioned), and the honest terminal run record, which is also the
        workflow's result."""
        return await workflow.execute_activity(
            "teardown",
            args=[run_id, handle, result, error],
            start_to_close_timeout=timedelta(seconds=self._profile.step_s),
            retry_policy=TEARDOWN_RETRY,
        )

    async def record_gate(self, run_id: str, gate: str, kind: str) -> None:
        """A gate transition as a first-class `process-gate` audit event —
        written by an activity, since workflow code cannot touch the
        registry."""
        await workflow.execute_activity(
            "record_gate_outcome",
            args=[run_id, gate, kind, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
            retry_policy=STEP_RETRY,
        )

    async def record_attempts(self, run_id: str) -> None:
        """After teardown of a run that concluded nothing — no PR delivered,
        no verdict reached — let the platform record whether that run spent
        the last attempt the blueprint allows on its issue or PR. Runs
        after teardown so the finished row counts, and before a cancelled
        run re-raises, so a cancel that exhausts an item is not silent."""
        await workflow.execute_activity(
            "record_attempts_exhausted",
            args=[run_id, workflow.info().workflow_id],
            start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
            retry_policy=STEP_RETRY,
        )

    async def wake_heartbeat(self) -> None:
        """Nudge the company's standing heartbeat that this run changed
        the world it watches. Doubly best-effort: the activity swallows
        its own failures, and the guard here means even a wedged
        connection cannot fail a run that already tore down — a missed
        nudge costs at most one reconcile interval."""
        try:
            await workflow.execute_activity(
                "wake_heartbeat",
                start_to_close_timeout=timedelta(seconds=BOOTSTRAP_S),
                retry_policy=SINGLE_ATTEMPT,
            )
        except ActivityError:
            workflow.logger.info("heartbeat wake failed; the reconcile timer covers it")

    async def dispatch(
        self,
        run_id: str,
        url: str,
        procedure: str,
        task_text: str,
        documents: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """One agent dispatch, budget-park aware: a `budget_exhausted`
        result parks the workflow on the `budget` signal in an API-visible
        state for the blueprint's `budgets.per_run.deadline_minutes`; an
        approving verdict re-dispatches the same text under the cap as the
        applied blueprint states it (a cap not raised parks it again),
        anything else returns the budget-marked result for teardown to
        finalize.
        Escalation and verdict are process-gate audit events, like the
        merge gate's — one Gate spine, one audit contract. Every dispatch
        carries the bounds the run's shape rendered — the charter as it
        binds this work, trusted like the vision; `documents` are the
        company's documents the procedure reads as untrusted context, and
        a procedure that judges nothing (deliver) carries none."""
        while True:
            result = await workflow.execute_activity(
                "dispatch_agent",
                args=[
                    run_id,
                    url,
                    procedure,
                    task_text,
                    self._config["bounds"],
                    documents or {},
                ],
                start_to_close_timeout=timedelta(seconds=self._profile.dispatch_s),
                schedule_to_close_timeout=timedelta(
                    seconds=self._profile.dispatch_schedule_to_close_s
                ),
                heartbeat_timeout=timedelta(seconds=self._profile.dispatch_heartbeat_s),
                retry_policy=DISPATCH_RETRY,
            )
            if BUDGET_MARKER not in ((result or {}).get("error") or ""):
                return result
            self._state = PARKED_STATE[BUDGET_GATE]
            outcome = await self._budget_gate.wait(
                **gate_deadline(self._config["budget_deadline_minutes"]),
                on_escalate=lambda: self.record_gate(run_id, BUDGET_GATE, GATE_ESCALATED),
            )
            self._state = "running"
            await self.record_gate(run_id, BUDGET_GATE, outcome.kind)
            if outcome.kind != GATE_APPROVED:
                return result
            # Approved: a fresh gate for any next park, then re-dispatch.
            self._budget_gate = Gate(BUDGET_GATE)
