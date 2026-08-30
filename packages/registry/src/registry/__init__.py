"""The registry: the join point between declarative intent and live state.

A shared library rather than a service: the writers are already separate
processes with separate identities, and putting an HTTP hop in front of
Postgres would add a failure mode without adding a boundary. One module
per table, each with exactly one writer; write ownership shows up as which
accessor a service imports — `Runs` for the orchestrator,
`SandboxRegistrations` and `AuditStore` for the gateway, `Companies`,
`AgentInstances` and `PolicyBundles` for the operator.
"""

from registry.audit import AuditStore
from registry.companies import Companies
from registry.crews import CrewRequests
from registry.db import Registry, dsn, open_registry
from registry.instances import AgentInstances
from registry.ledger import BudgetLedger
from registry.participants import ParticipantRevoked, RunParticipants
from registry.policy import PolicyBundles, StoredBundle, pack
from registry.runs import ISSUE_TASK_RE, REVIEW_TASK_RE, Attempts, RunRecord, Runs
from registry.sandboxes import SandboxRegistrations

__all__ = [
    "AgentInstances",
    "AuditStore",
    "BudgetLedger",
    "Companies",
    "CrewRequests",
    "ParticipantRevoked",
    "PolicyBundles",
    "Registry",
    "RunParticipants",
    "Attempts",
    "ISSUE_TASK_RE",
    "REVIEW_TASK_RE",
    "RunRecord",
    "Runs",
    "SandboxRegistrations",
    "StoredBundle",
    "dsn",
    "open_registry",
    "pack",
]
