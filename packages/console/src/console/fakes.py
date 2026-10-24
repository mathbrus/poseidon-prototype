"""A console over in-memory stand-ins — for tests and the interface's
golden examples only.

Nothing in a running service may import this module. It lives beside the
console, like `registry.memory` beside the registry, because two
consumers need the same double: the console's own tests, and
`scripts/console_interface.py`, which renders one golden example per
route from it for the CLI's tests to replay.

`World` holds the registry fakes, a Temporal that records what it was
asked, an OPA that answers what the compiled bundle would for the
declared humans, and a Company CR; `World.app()` is the console over
them. Every clock in it is the world's own, so what it answers is the
same on every run.
"""

from collections.abc import Callable
from functools import cache
from pathlib import Path
from typing import Any

import httpx
import yaml
from fastapi import FastAPI

from common.audit import AuditEvent
from common.authn import Principal
from common.console import EXECUTION_RUNNING
from common.policy import COMPANY_DELETE, DEFAULT_DENY, GATE_RESOLVE, verdict_rule
from common.topology import COMPANY_NAMESPACE, CONSOLE_TOKEN_AUDIENCE, PLATFORM_NAMESPACE
from common.workflows import (
    HEARTBEAT_PAUSED,
    HEARTBEAT_WORKFLOW,
    RUN_RUNNING,
    workflow_id,
)
from compiler import DeploymentPlan, compile_blueprint
from console import crs
from console import service as service_module
from console.app import create_app
from console.config import ConsoleSettings
from console.gateway import GatewayUnavailable
from console.temporal import WorkflowNotFound
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryCompanySweep,
    InMemoryFiledTickets,
    InMemoryRunParticipants,
    InMemoryRuns,
)

COMPANY = "hello-saas"
# The example the world's company is applied from.
HELLO_SAAS = Path(__file__).resolve().parents[4] / "examples" / "hello-saas.yaml"
NOW = "2026-01-01T00:00:00+00:00"
HEARTBEAT_PREFIX = workflow_id(HEARTBEAT_WORKFLOW, "")
# The tokens the fake TokenReview knows: the human the company names, a
# human the console may declare but the company does not name, a human the
# console does not declare, and an agent's workload token.
OWNER_TOKEN = "t-owner"
AUDITOR_TOKEN = "t-auditor"
STRANGER_TOKEN = "t-stranger"
AGENT_TOKEN = "t-agent"
# The humans the world's company names (`company.humans`).
NAMED = frozenset({"owner"})


def as_owner() -> dict[str, str]:
    return {"Authorization": f"Bearer {OWNER_TOKEN}"}


class FakeReviewer:
    async def review(self, token: str, audience: str) -> Principal | None:
        if audience != CONSOLE_TOKEN_AUDIENCE:
            return None
        return {
            OWNER_TOKEN: Principal.of(
                f"system:serviceaccount:{PLATFORM_NAMESPACE}:human-owner"
            ),
            AUDITOR_TOKEN: Principal.of(
                f"system:serviceaccount:{PLATFORM_NAMESPACE}:human-auditor"
            ),
            STRANGER_TOKEN: Principal.of(
                f"system:serviceaccount:{PLATFORM_NAMESPACE}:human-x"
            ),
            AGENT_TOKEN: Principal.of(f"system:serviceaccount:{COMPANY}:pm-1"),
        }.get(token)


class FakeWorkflows:
    """Records what the console asked Temporal for, in order. `running`
    maps a workflow id to the state its `state()` query answers; a
    cancelled or terminated workflow closes at once. `held` says whether
    the company's heartbeat would read a pause: a wake then answers
    `paused`, as the loop's next tick would."""

    def __init__(self, held: Callable[[], bool] = lambda: False) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.running: dict[str, str] = {}
        self.closed: dict[str, str] = {}
        self._held = held

    async def start(self, workflow: str, key: str, payload: dict[str, Any]) -> str:
        wf_id = workflow_id(workflow, key)
        self.calls.append(("start", (workflow, key, payload)))
        self.running[wf_id] = RUN_RUNNING
        return wf_id

    async def signal(self, wf_id: str, name: str, args: list[Any]) -> None:
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        self.calls.append(("signal", (wf_id, name, args)))
        if name == "wake" and wf_id.startswith(HEARTBEAT_PREFIX) and self._held():
            self.running[wf_id] = HEARTBEAT_PAUSED

    async def cancel(self, wf_id: str) -> None:
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        self.calls.append(("cancel", wf_id))
        del self.running[wf_id]
        self.closed[wf_id] = "CANCELED"

    async def terminate(self, wf_id: str, reason: str) -> None:
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        self.calls.append(("terminate", wf_id))
        del self.running[wf_id]
        self.closed[wf_id] = "TERMINATED"

    async def delete(self, wf_id: str) -> None:
        self.calls.append(("delete", wf_id))
        self.running.pop(wf_id, None)
        self.closed.pop(wf_id, None)

    async def inspect(self, wf_id: str) -> dict[str, Any]:
        if wf_id in self.closed:
            return {"workflow_id": wf_id, "status": self.closed[wf_id], "state": None}
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        return {
            "workflow_id": wf_id,
            "status": EXECUTION_RUNNING,
            "state": self.running[wf_id],
        }


class FakeGateway:
    """The gateway's `repo.read_file` over a dict of path to text, and its
    `repo.purge`; records what it was asked. `purge_refusal` is what the
    git host answers a purge with, when it refuses one."""

    def __init__(self, on_purge: Callable[[], None] = lambda: None) -> None:
        self.files: dict[str, str] = {}
        self.reads: list[tuple[str, str, str]] = []
        self.purges: list[tuple[str, str]] = []
        self.purge_refusal: str | None = None
        self._on_purge = on_purge

    async def read(self, repo: str, ref: str, path: str) -> dict[str, Any]:
        self.reads.append((repo, ref, path))
        text = self.files.get(path)
        return {"present": text is not None, "text": text}

    async def purge(self, repo: str, base_ref: str) -> dict[str, Any]:
        if self.purge_refusal is not None:
            raise GatewayUnavailable(
                f"mcp gateway refused repo.purge: {self.purge_refusal}"
            )
        self.purges.append((repo, base_ref))
        self._on_purge()
        return {"reset": [base_ref], "commit": "r00t"}


class FixedCompanies(InMemoryCompanies):
    """The registry's companies, with the pause stamped at the world's
    clock rather than the host's."""

    def __init__(self, clock: Callable[[], str]) -> None:
        super().__init__()
        self._clock = clock

    def pause(self, name: str, by: str) -> dict[str, Any]:
        self.pauses[name] = {"by": by, "at": self._clock()}
        return self.pauses[name]


@cache
def applied_spec() -> dict[str, Any]:
    """What the reconciler stores in `companies` for hello-saas: the
    Company CR's spec, less the bundle."""
    plan = compile_blueprint(yaml.safe_load(HELLO_SAAS.read_text()))
    spec = crs.company_cr(plan)["spec"]
    return {k: v for k, v in spec.items() if k != "policyBundle"}


def company_cr(name: str, generation: int = 1, **status: Any) -> dict[str, Any]:
    """A Company CR as the cluster holds it."""
    return {
        "metadata": {"name": name, "generation": generation},
        "spec": {"namespace": COMPANY_NAMESPACE},
        "status": status,
    }


class World:
    def __init__(self) -> None:
        self.now = NOW
        # The humans the console declares (`CONSOLE_HUMANS`).
        self.humans = "owner"
        self.audit = InMemoryAuditStore()
        self.runs = InMemoryRuns()
        self.ledger = InMemoryBudgetLedger()
        self.instances = InMemoryAgentInstances()
        self.participants = InMemoryRunParticipants()
        self.tickets = InMemoryFiledTickets()
        # The reconciler's record of the applied company; the console reads
        # which company it hosts from here.
        self.companies = FixedCompanies(lambda: self.now)
        self.companies.upsert(COMPANY, applied_spec())
        self.workflows = FakeWorkflows(
            held=lambda: self.companies.paused(COMPANY) is not None
        )
        self.gateway = FakeGateway(on_purge=self._purged)
        self.submitted: list[DeploymentPlan] = []
        self.deleted: list[str] = []
        # What the world looked like when the Company CR went: the rows the
        # sweep erased, in the order written, the pause, and what Temporal
        # still ran.
        self.erased: list[AuditEvent] = []
        self.paused_at_delete: dict[str, Any] | None = None
        self.running_at_delete: dict[str, str] = {}
        self.purged_at_delete: list[tuple[str, str]] = []
        # What Temporal still ran when the repository was purged.
        self.running_at_purge: dict[str, str] | None = None
        self.cr: dict[str, Any] | None = None
        # What OPA answers, per tool; the default is the rule the compiled
        # bundle would name: the act's for a human the company names,
        # `human_not_recognized` for any other.
        self.decisions: dict[str, dict[str, Any]] = {}
        self.asked: list[dict[str, Any]] = []
        # The run ids the console mints, in order, when a run is started.
        self.run_ids: list[str] = []

    # -- the doubles the console is built over -------------------------------

    def submit(
        self, plan: DeploymentPlan, source: dict[str, str]
    ) -> tuple[int, int, int]:
        self.submitted.append(plan)
        # What the CR says afterwards: the generation this apply made and
        # the text applied, with no status until the fake reconciler is
        # told otherwise.
        self.cr = company_cr(plan.company.name, generation=len(self.submitted))
        self.cr["spec"]["blueprintSource"] = source
        return (1, 0, len(self.submitted))

    def read_company(self) -> dict[str, Any] | None:
        return self.cr

    def delete(self, company: str, timeout_s: float) -> None:
        """The Company CR deleted and the reconciler's teardown done: the
        sweep over every double the world holds. What Temporal still runs
        is what the console must end."""
        self.deleted.append(company)
        self.cr = None
        self.erased = list(self.audit.events)
        self.paused_at_delete = self.companies.paused(company)
        self.running_at_delete = dict(self.workflows.running)
        self.purged_at_delete = list(self.gateway.purges)
        InMemoryCompanySweep(
            companies=self.companies,
            instances=self.instances,
            runs=self.runs,
            participants=self.participants,
            tickets=self.tickets,
            ledger=self.ledger,
            audit=self.audit,
        ).sweep(company)

    def _purged(self) -> None:
        self.running_at_purge = dict(self.workflows.running)

    async def evaluate(self, opa_url: str, company: str, decision_input: dict) -> dict:
        self.asked.append(decision_input)
        tool = decision_input["tool"]
        if tool in self.decisions:
            return dict(self.decisions[tool])
        if decision_input["agent_id"] not in NAMED:
            return {"allow": False, "rule": "human_not_recognized"}
        args = decision_input["args"]
        if tool == GATE_RESOLVE:
            return {"allow": True, "rule": verdict_rule(args["gate"], args["approved"])}
        if tool == COMPANY_DELETE:
            purge = args.get("purge_remote")
            if not isinstance(purge, bool):
                return {"allow": False, "rule": DEFAULT_DENY}
            rule = "company_delete_purge_remote" if purge else "company_delete"
            return {"allow": True, "rule": rule}
        return {"allow": True, "rule": tool.replace(".", "_")}

    def new_run_id(self) -> str:
        return self.run_ids.pop(0) if self.run_ids else "run-01STARTED"

    def patch(self, setattr_: Callable[[Any, str, Any], None]) -> None:
        """Point the service's OPA and run-id minting at this world —
        with pytest's `monkeypatch.setattr` in a test, `setattr` in a
        script."""
        setattr_(service_module, "evaluate_decision", self.evaluate)
        setattr_(service_module, "new_run_id", self.new_run_id)

    # -- the console over them ----------------------------------------------

    def app(self) -> FastAPI:
        settings = ConsoleSettings(
            platform_namespace=PLATFORM_NAMESPACE,
            opa_url="http://opa.test:8181",
            temporal_address="temporal.test:7233",
            console_humans=self.humans,
        )
        return create_app(
            settings,
            audit=self.audit,
            runs=self.runs,
            ledger=self.ledger,
            instances=self.instances,
            companies=self.companies,
            participants=self.participants,
            tickets=self.tickets,
            token_reviewer=FakeReviewer(),
            workflows=self.workflows,
            gateway=self.gateway,
            submit_crs=self.submit,
            delete_company=self.delete,
            read_company=self.read_company,
            clock=lambda: self.now,
        )

    def client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            transport=httpx.ASGITransport(app=self.app()), base_url="http://console.test"
        )

    @property
    def events(self) -> list[tuple[str, str, str]]:
        return [(e.tool, e.decision, e.rule or "") for e in self.audit.events]
