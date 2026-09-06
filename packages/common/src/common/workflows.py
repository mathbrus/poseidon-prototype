"""Where the workflows run and how they are named — shared by the worker
that hosts them and every client that starts, signals or inspects them
(the orchestrator's own activities, the console).

Two components must agree on these, which is why they live here and not
in the orchestrator: a console that addressed `implement-{run_id}` on the
wrong task queue would fail silently, and a heartbeat audit story keyed
on a different id than the one the loop writes would be empty.
"""

# The workflows a run id (or, for the heartbeat, a company) addresses.
# The `heartbeat` workflow is the company's standing loop — one durable
# workflow per company. It is unrelated to Temporal's activity heartbeats,
# which are liveness pings on a dispatch.
WORKFLOWS = ("scope", "implement", "review", "heartbeat")

# The run-shaped workflows a human may start by hand on a text the company
# produced: an implement run on a ticket, a review run on a pull request.
# `scope` reads the observation and takes no text, so nothing starts it
# but the heartbeat.
HUMAN_STARTABLE = ("implement", "review")

# The gates a run parks on for a human: the merge gate of a review, and
# the budget pause of any run that hit its cap. A human resolves either
# through the console; the compiler emits one rule per gate and verdict.
MERGE_GATE = "merge"
BUDGET_GATE = "budget"
GATES = (MERGE_GATE, BUDGET_GATE)

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
