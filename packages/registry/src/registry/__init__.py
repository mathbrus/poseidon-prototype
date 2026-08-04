"""The registry (overview §4.1, plan step 8): the join point between
declarative intent and live state.

A shared library rather than a service: the writers are already separate
processes with separate identities, and putting an HTTP hop in front of
Postgres would add a failure mode without adding a boundary. Write
ownership shows up as which accessor a service imports — `Runs` for the
orchestrator, `SandboxRegistrations` and `AuditStore` for the gateway — and
each table has exactly one writer.
"""

from registry.audit import AuditStore
from registry.crews import CrewRequests
from registry.db import Registry, dsn, open_registry
from registry.ledger import BudgetLedger
from registry.participants import RunParticipants
from registry.runs import RunRecord, Runs
from registry.sandboxes import SandboxRegistration, SandboxRegistrations

__all__ = [
    "AuditStore",
    "BudgetLedger",
    "CrewRequests",
    "Registry",
    "RunParticipants",
    "RunRecord",
    "Runs",
    "SandboxRegistration",
    "SandboxRegistrations",
    "dsn",
    "open_registry",
]
