"""Each act's two checks, spelled once: the arguments its decision input
carries, and the state the company must be in for it.

An act and its preview call the same function for each, so they cannot
drift: the act asks policy with these arguments and raises `Invalid` on
the state's refusal; the preview (`GET /acts`, `GET /runs/{run_id}/acts`)
asks the same policy question and reports the same refusal, without
recording or doing anything. The state is checked first, as the act
checks it: a question nobody asked (a verdict on a run not parked) is
refused before policy is consulted, and before anything is recorded.

The arguments are what policy decides on and nothing more: a person's
rationale is not among them — it rides the audit row in its own column.
"""

from typing import Any

from common.console import EXECUTION_RUNNING, ExecutionView
from common.policy import DEFAULT_DENY
from common.workflows import GATES_OF, PARKED_STATE
from registry import RunRecord

# -- what policy answered, as the act and its preview both say it -------------


def refused_rule(decision: dict[str, Any]) -> str | None:
    """The rule a decision refused under, or None when it allowed."""
    if decision.get("allow"):
        return None
    return str(decision.get("rule") or DEFAULT_DENY)


def policy_detail(rule: str) -> str:
    """How a policy refusal reads: the 403's detail."""
    return f"denied by policy: {rule}"


# -- the decision input's arguments, per act ----------------------------------


def apply_args(company: str) -> dict[str, Any]:
    return {"company": company}


def start_args(workflow: str, number: int | None) -> dict[str, Any]:
    """The run a human starts; a preview, knowing no number, passes None —
    the compiled rule keys on the workflow alone."""
    return {"workflow": workflow, "number": number}


def verdict_args(gate: str, run_id: str, approved: bool) -> dict[str, Any]:
    return {"gate": gate, "run_id": run_id, "approved": approved}


def cancel_args(run_id: str) -> dict[str, Any]:
    return {"run_id": run_id}


# Wake, pause, resume and delete: the act is the fact.
NO_ARGS: dict[str, Any] = {}


# -- the state each act needs, as the refusal when it is missing --------------


def verdict_refusal(record: RunRecord, gate: str, execution: ExecutionView) -> str | None:
    """A verdict answers a question that was asked: the run's workflow has
    the gate, and the run is parked on it."""
    if gate not in GATES_OF.get(record.workflow, ()):
        return f"a {record.workflow} run has no {gate} gate"
    if execution.status != EXECUTION_RUNNING:
        return f"run {record.run_id!r} has no workflow to signal"
    state = execution.state.value if execution.state else None
    if state != PARKED_STATE[gate]:
        return f"run {record.run_id!r} is not parked on its {gate} gate (state {state!r})"
    return None


def cancel_refusal(record: RunRecord, execution: ExecutionView) -> str | None:
    """A cancel stops a workflow that runs."""
    if execution.status != EXECUTION_RUNNING:
        return f"run {record.run_id!r} has no workflow to cancel"
    return None


def wake_refusal(company: str, execution: ExecutionView) -> str | None:
    """A wake nudges a loop that runs."""
    if execution.status != EXECUTION_RUNNING:
        return f"no heartbeat is running for {company!r}"
    return None


def pause_refusal(company: str, paused: dict[str, Any] | None) -> str | None:
    return f"{company!r} is already paused" if paused else None


def resume_refusal(company: str, paused: dict[str, Any] | None) -> str | None:
    return None if paused else f"{company!r} is not paused"
