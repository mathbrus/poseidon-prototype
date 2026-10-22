"""The console serves the console interface and nothing else.

- only `views.py` translates internals (rows, records, Temporal's answers,
  the CR) into interface models; `service.py` builds no response dicts;
- every served route declares its response model from `common.console`,
  and the served set is the interface's route table;
- the closed sets the console fills from Temporal match Temporal's.
"""

import ast
from pathlib import Path

from temporalio.client import WorkflowExecutionStatus

from common.audit import AuditEvent
from common.console import ROUTES, AuditEventView, ExecutionStatus, enums, models
from console import views
from console.app import create_app
from console.config import ConsoleSettings
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRuns,
)

SOURCE = Path(views.__file__).parent


def test_the_service_builds_no_response_dicts():
    """A dict returned from the service, a record's `as_dict()` spread
    into one, or a row passed through untouched would be an internal
    shape reaching a client: the mapping is `views.py`'s alone."""
    tree = ast.parse((SOURCE / "service.py").read_text())
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Dict):
            raise AssertionError(f"service.py:{node.lineno} returns a dict")
        if isinstance(node, ast.Attribute) and node.attr == "as_dict":
            raise AssertionError(f"service.py:{node.lineno} reads a record's dict")


def test_every_served_route_declares_its_interface_model():
    app = create_app(
        ConsoleSettings(
            platform_namespace="poseidon-platform",
            opa_url="http://opa.test:8181",
            temporal_address="temporal.test:7233",
        ),
        audit=InMemoryAuditStore(),
        runs=InMemoryRuns(),
        ledger=InMemoryBudgetLedger(),
        instances=InMemoryAgentInstances(),
        companies=InMemoryCompanies(),
        workflows=object(),
        submit_crs=lambda plan: (0, 0, 1),
        read_company=lambda: None,
        clock=lambda: "",
    )
    served = {
        (method, route.path): route.response_model
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if method != "HEAD" and not route.path.startswith(("/docs", "/redoc", "/openapi"))
    }
    assert set(served) == {(r.method, r.path) for r in ROUTES}
    for entry in ROUTES:
        assert served[(entry.method, entry.path)] is entry.response, entry
        assert entry.response.__module__ == models.__name__


def test_temporals_statuses_are_the_interfaces():
    assert enums.TEMPORAL_STATUSES == tuple(s.name for s in WorkflowExecutionStatus)
    assert {m.value for m in ExecutionStatus} == {
        *enums.TEMPORAL_STATUSES,
        enums.EXECUTION_NOT_FOUND,
    }


def test_an_audit_row_reaches_a_client_without_its_digest():
    """The arguments' digest and the backend's status are the ledger's,
    not a reader's."""
    audit = InMemoryAuditStore()
    audit.append(
        AuditEvent(
            ts="2026-01-01T00:00:00+00:00",
            run_id="run-1",
            agent_id="pm-1",
            role="pm",
            tool="git.read",
            args_digest="d",
            decision="allow",
            rule="r",
            latency_ms=3,
            backend_status="ok",
            source="gateway",
            company="acme",
        )
    )
    (row,) = audit.story("run-1")
    view = views.event_view(row)
    assert isinstance(view, AuditEventView) and view.id == 1
    dumped = view.model_dump()
    assert "args_digest" not in dumped and "backend_status" not in dumped
