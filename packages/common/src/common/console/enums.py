"""The closed sets the console interface names, built from the constants
every writer spells them from — `common.workflows`, `common.audit`,
`common.topology` — so a value the platform starts writing is a member
here in the same change, or a failing test.

Each is a `StrEnum`: its members compare equal to the strings the
writers use, and its OpenAPI rendering lists every value.
"""

from enum import StrEnum

from common.audit import CONSOLE_ACTS, DECISIONS, SOURCES
from common.topology import COMPANY_PHASES
from common.workflows import (
    GATE_OUTCOMES,
    GATES,
    HEARTBEAT_STATES,
    HUMAN_STARTABLE,
    RUN_STATES,
    RUN_STATUSES,
    TICK_OUTCOMES,
    WORKFLOWS,
)


def _closed(name: str, values: tuple[str, ...]) -> type[StrEnum]:
    """A StrEnum whose members are `values`, named as identifiers."""
    return StrEnum(name, {v.replace("-", "_").replace(".", "_"): v for v in values})


RunStatus = _closed("RunStatus", RUN_STATUSES)
RunState = _closed("RunState", RUN_STATES)
HeartbeatState = _closed("HeartbeatState", HEARTBEAT_STATES)
Gate = _closed("Gate", GATES)
GateOutcome = _closed("GateOutcome", GATE_OUTCOMES)
TickOutcome = _closed("TickOutcome", TICK_OUTCOMES)
Workflow = _closed("Workflow", WORKFLOWS)
StartableWorkflow = _closed("StartableWorkflow", HUMAN_STARTABLE)
AuditSource = _closed("AuditSource", SOURCES)
Decision = _closed("Decision", DECISIONS)
CompanyPhase = _closed("CompanyPhase", COMPANY_PHASES)
Act = _closed("Act", CONSOLE_ACTS)
# Why an act's preview says no: policy refuses this person, or the
# company's state refuses the act (a verdict on a run not parked, a pause
# of a paused company).
REFUSAL_KINDS = ("policy", "state")
RefusalKind = _closed("RefusalKind", REFUSAL_KINDS)

# The company's two documents, by the names a blueprint places them under
# (`blueprint.documents.DOCUMENT_NAMES`, a package `common` does not
# depend on; the console's tests hold the two equal).
DOCUMENT_KINDS = ("strategy", "architecture")
DocumentKind = _closed("DocumentKind", DOCUMENT_KINDS)

# Temporal's execution statuses by name (`temporalio.client.
# WorkflowExecutionStatus`, which `common` does not depend on; the
# console's tests hold the two equal), plus the console's answer for an
# execution Temporal does not know.
TEMPORAL_STATUSES = (
    "RUNNING",
    "COMPLETED",
    "FAILED",
    "CANCELED",
    "TERMINATED",
    "CONTINUED_AS_NEW",
    "TIMED_OUT",
)
EXECUTION_RUNNING = "RUNNING"
EXECUTION_NOT_FOUND = "NOT_FOUND"
ExecutionStatus = _closed("ExecutionStatus", (*TEMPORAL_STATUSES, EXECUTION_NOT_FOUND))
