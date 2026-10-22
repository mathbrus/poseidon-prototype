"""Where the workflows run and how they are named — shared by the worker
that hosts them and every client that starts, signals or inspects them
(the orchestrator's own activities, the console).

Two components must agree on these, which is why they live here and not
in the orchestrator: a console that addressed `implement-{run_id}` on the
wrong task queue would fail silently, and a heartbeat audit story keyed
on a different id than the one the loop writes would be empty.
"""

from datetime import timedelta

# The workflows a run id (or, for the heartbeat, a company) addresses.
# The `heartbeat` workflow is the company's standing loop — one durable
# workflow per company. It is unrelated to Temporal's activity heartbeats,
# which are liveness pings on a dispatch.
# The standing loop's workflow name — the `workflow` column of its runs
# and rows — and the run-shaped three it starts.
HEARTBEAT_WORKFLOW = "heartbeat"
RUN_WORKFLOWS = ("scope", "implement", "review")
WORKFLOWS = (*RUN_WORKFLOWS, HEARTBEAT_WORKFLOW)

# The run-shaped workflows a human may start by hand on a text the company
# produced: an implement run on a ticket, a review run on a pull request.
# `scope` reads the observation and takes no text, so nothing starts it
# but the heartbeat.
HUMAN_STARTABLE = ("implement", "review")

# The gates a run parks on for a human: the merge gate of a review, and
# the budget park of any run that hit its cap. A human resolves either
# through the console; the compiler emits one rule per gate and verdict.
MERGE_GATE = "merge"
BUDGET_GATE = "budget"
GATES = (MERGE_GATE, BUDGET_GATE)
# The halts the heartbeat records in the same shape — a `gate.<name>`
# process-gate event — but nobody resolves: a run spent the last attempt
# the blueprint allows on its text, the bootstrap ticket exhausted its
# attempts and the company cannot write its own documents, or the daily
# spend cap halted every start until tomorrow. The world lifts a halt;
# no verdict does.
ATTEMPTS_GATE = "heartbeat_attempts"
BOOTSTRAP_GATE = "heartbeat_bootstrap_exhausted"
DAILY_CAP_GATE = "heartbeat_daily_cap"


def gate_timer(deadline_minutes: int) -> tuple[timedelta, timedelta]:
    """A gate's timer from its declared wait: `(escalate_after, expire_after)`
    — escalation halfway, expiry at the deadline. The workflow parks on it;
    the console says when a parked run escalates and expires by it."""
    return timedelta(seconds=deadline_minutes * 30), timedelta(minutes=deadline_minutes)


# Which gates each run-shaped workflow has: a verdict on a gate a run
# cannot park on is refused before policy is asked.
GATES_OF = {
    "scope": (BUDGET_GATE,),
    "implement": (BUDGET_GATE,),
    "review": (BUDGET_GATE, MERGE_GATE),
}
# The API-visible state a workflow reports while parked on a gate — what
# the console requires before it records a verdict, so an answer is only
# ever given to a question that was asked. One prefix, so a reader knows
# a parked run by its state's first word.
PARKED_PREFIX = "parked-"
PARKED_STATE = {
    MERGE_GATE: f"{PARKED_PREFIX}merge_gate",
    BUDGET_GATE: f"{PARKED_PREFIX}budget_exhausted",
}

# Every state a run-shaped workflow's `state()` query answers: working,
# parked on a gate, or finalizing (tearing its crew and sandbox down).
# The console passes it through as the execution's state; a workflow
# that returned anything else would break every reader of it.
RUN_RUNNING = "running"
RUN_FINALIZING = "finalizing"
RUN_STATES = (RUN_RUNNING, *PARKED_STATE.values(), RUN_FINALIZING)
# Every state the heartbeat's `state()` query answers: sleeping between
# ticks, ticking while a tick runs, paused while a human holds it.
HEARTBEAT_SLEEPING = "sleeping"
HEARTBEAT_TICKING = "ticking"
HEARTBEAT_PAUSED = "paused"
HEARTBEAT_STATES = (HEARTBEAT_SLEEPING, HEARTBEAT_TICKING, HEARTBEAT_PAUSED)

# What a run's registry row says of it: running until its teardown
# writes the terminal status, done or failed. Not the workflow's state:
# a run parked on a gate is still `running` here.
RUN_STATUS_RUNNING = "running"
RUN_STATUS_DONE = "done"
RUN_STATUS_FAILED = "failed"
RUN_STATUSES = (RUN_STATUS_RUNNING, RUN_STATUS_DONE, RUN_STATUS_FAILED)

# What happened at a gate: one outcome per kind, written on the gate's
# process-gate audit row as `gate_<kind>` by the orchestrator and read
# back by the console. A run began waiting on it (parked — the row a
# parked run's deadlines are measured from), a human answered (approved,
# rejected), nobody did
# (escalated at half the deadline, expired at the end), policy answered
# the merge (resolved it, or declined and fell back to the human), the
# review filed a rework ticket or spent its last rework, an approving
# signal arrived without a recorded verdict, the heartbeat halted a start
# on a gate nobody resolves, or a text spent its last attempt.
GATE_PARKED = "parked"
GATE_APPROVED = "approved"
GATE_REJECTED = "rejected"
GATE_ESCALATED = "escalated"
GATE_EXPIRED = "expired"
GATE_POLICY_RESOLVED = "policy_resolved"
GATE_POLICY_DECLINED = "policy_declined"
GATE_REWORK_FILED = "rework_filed"
GATE_REWORK_EXHAUSTED = "rework_exhausted"
GATE_VERDICT_UNVERIFIED = "verdict_unverified"
GATE_HALTED = "halted"
GATE_EXHAUSTED = "exhausted"
GATE_OUTCOMES = (
    GATE_PARKED,
    GATE_APPROVED,
    GATE_REJECTED,
    GATE_ESCALATED,
    GATE_EXPIRED,
    GATE_POLICY_RESOLVED,
    GATE_POLICY_DECLINED,
    GATE_REWORK_FILED,
    GATE_REWORK_EXHAUSTED,
    GATE_VERDICT_UNVERIFIED,
    GATE_HALTED,
    GATE_EXHAUSTED,
)
# The outcomes that release what the gate held — the only `allow` rows a
# gate writes. Every other outcome is a `deny`.
GATE_RELEASING = (GATE_APPROVED, GATE_POLICY_RESOLVED)
_GATE_PREFIX = "gate_"


def gate_rule(kind: str) -> str:
    """The rule a gate outcome's audit row carries."""
    if kind not in GATE_OUTCOMES:
        raise ValueError(f"not a gate outcome: {kind!r}")
    return f"{_GATE_PREFIX}{kind}"


# What a heartbeat tick records about itself: one outcome and a detail,
# written on the tick's audit row as `tick_<outcome>:<detail>` by the
# orchestrator and read back by the CLI. A tick started work, was idle
# (and the detail says why), was refused (by policy or a failed step),
# stopped because the company is gone, or found the company paused (the
# detail names who paused it).
TICK_STARTED = "started"
TICK_IDLE = "idle"
TICK_REFUSED = "refused"
TICK_STOPPED = "stopped"
TICK_PAUSED = "paused"
TICK_OUTCOMES = (TICK_STARTED, TICK_IDLE, TICK_REFUSED, TICK_STOPPED, TICK_PAUSED)
_TICK_PREFIX = "tick_"


def tick_rule(outcome: str, detail: str) -> str:
    """The rule a tick's audit row carries."""
    if outcome not in TICK_OUTCOMES:
        raise ValueError(f"not a tick outcome: {outcome!r}")
    return f"{_TICK_PREFIX}{outcome}:{detail}"


def parse_tick_rule(rule: str) -> tuple[str, str]:
    """`(outcome, detail)` from a tick row's rule."""
    outcome, _, detail = rule.removeprefix(_TICK_PREFIX).partition(":")
    return outcome, detail


# One Temporal namespace, one task queue.
TEMPORAL_NAMESPACE = "default"
TASK_QUEUE = "poseidon-orchestrator"


def workflow_id(workflow: str, key: str) -> str:
    """Deterministic workflow naming. The run-shaped workflows run one
    workflow per run (`implement-{run_id}`); the heartbeat is one
    standing workflow per company (`heartbeat-{company}`), keyed by company
    because the loop is standing structure and its audit story correlates
    on that id."""
    return f"{workflow}-{key}"


def heartbeat_run_id(company: str) -> str:
    """The run id every heartbeat read, filing and audit row is
    attributed to: the company's one standing loop, by its workflow id."""
    return workflow_id("heartbeat", company)
