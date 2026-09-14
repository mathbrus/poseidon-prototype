"""The console's HTTP surface.

JSON in, JSON out, a bearer token on every request but the health probe.
Acts are POSTs and answer 403 with the denying rule when policy says no;
reads are GETs. 422 names what could not be acted on (an unknown run, a
blueprint that does not validate), 503 says Temporal was unreachable.
The route table below is what the reference documentation renders.

Run with: uvicorn --factory console.app:create_app
"""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from common.audit import RATIONALE_LIMIT
from common.authn import Authenticator, AuthMiddleware, Reviewer, default_reviewer
from common.topology import CONSOLE_TOKEN_AUDIENCE
from compiler import DeploymentPlan
from console import crs
from console.config import ConsoleSettings
from console.service import ConsoleService, Invalid, NoCompany, PolicyDenied
from console.temporal import TemporalWorkflows, Unavailable, Workflows
from registry import (
    AgentInstances,
    AuditStore,
    BudgetLedger,
    Companies,
    Runs,
    open_registry,
)

# The routes, for the documentation: method, path, what it does, the
# decision an act must win (reads decide nothing).
ROUTES: tuple[tuple[str, str, str, str], ...] = (
    ("GET", "/healthz", "liveness; the one unauthenticated route", ""),
    ("GET", "/whoami", "the verified human and the applied company, if any", ""),
    (
        "POST",
        "/blueprint/preview",
        "compile against the live registry; the diff and the plan",
        "",
    ),
    (
        "POST",
        "/blueprint/apply",
        "compile and submit the Company and AgentInstance CRs",
        "blueprint.apply",
    ),
    (
        "GET",
        "/company",
        "the applied company's phase, generation and bundle revision, from its CR",
        "",
    ),
    (
        "DELETE",
        "/company",
        "stop the company: cancel its runs, delete its CR, wait for the teardown",
        "company.delete",
    ),
    ("POST", "/runs", "start one implement or review run by hand", "run.start"),
    ("GET", "/runs", "the company's runs since a fence (`?since=`)", ""),
    ("GET", "/runs/{run_id}", "the run record and its workflow's execution", ""),
    (
        "GET",
        "/runs/{run_id}/story",
        "the run's audit story, every source; `?decisions=true` keeps the rows"
        " carrying a rationale",
        "",
    ),
    ("GET", "/runs/{run_id}/spend", "the run's attributed spend", ""),
    (
        "POST",
        "/runs/{run_id}/gates/{gate}",
        "record a verdict, then deliver it",
        "gate.resolve",
    ),
    ("POST", "/runs/{run_id}/cancel", "cancel a run; teardown compensates", "run.cancel"),
    (
        "GET",
        "/heartbeat",
        "the standing loop's execution and its ticks since a fence",
        "",
    ),
    ("POST", "/heartbeat/wake", "nudge the loop to look now", "heartbeat.wake"),
    ("GET", "/spend", "the company's spend since a fence", ""),
    (
        "GET",
        "/events",
        "the company's audit rows after a cursor (`?after=&run=`), with its spend to date",
        "",
    ),
    ("GET", "/now", "the registry's clock: the fence to take before acting", ""),
)


class BlueprintRequest(BaseModel):
    blueprint: str


class StartRunRequest(BaseModel):
    workflow: str
    number: int


# A person's reason is part of the act: a verdict, a cancel or a delete
# without one is refused, and the reason is stored beside the row as its
# rationale.
Reason = Field(min_length=1, max_length=RATIONALE_LIMIT)


class VerdictRequest(BaseModel):
    approved: bool
    reason: str = Reason


class CancelRequest(BaseModel):
    reason: str = Reason


class DeleteRequest(BaseModel):
    reason: str = Reason


def create_app(
    settings: ConsoleSettings | None = None,
    audit: AuditStore | None = None,
    runs: Runs | None = None,
    ledger: BudgetLedger | None = None,
    instances: AgentInstances | None = None,
    companies: Companies | None = None,
    token_reviewer: Reviewer | None = None,
    workflows: Workflows | None = None,
    submit_crs: Callable[[DeploymentPlan], tuple[int, int, int]] | None = None,
    delete_company: Callable[[str], None] | None = None,
    read_company: Callable[[], dict[str, Any] | None] | None = None,
    clock: Callable[[], str] | None = None,
) -> FastAPI:
    settings = settings or ConsoleSettings()
    accessors = (audit, runs, ledger, instances, companies)
    registry = None
    if all(a is None for a in accessors):
        registry = open_registry()
        audit = AuditStore(registry)
        runs = Runs(registry)
        ledger = BudgetLedger(registry)
        instances = AgentInstances(registry)
        companies = Companies(registry)
        clock = clock or registry.now
    elif any(a is None for a in accessors):
        raise ValueError("inject all registry accessors, or none")
    if clock is None:
        raise ValueError("inject a clock with the registry accessors")

    authenticator = Authenticator(
        settings,
        audit,
        reviewer=token_reviewer or default_reviewer(settings),
        source="console",
        audience=CONSOLE_TOKEN_AUDIENCE,
        humans=settings.humans,
    )
    service = ConsoleService(
        settings,
        audit,
        runs,
        ledger,
        instances,
        companies,
        workflows or TemporalWorkflows(settings.temporal_address),
        submit_crs or crs.submit,
        delete_company or crs.delete_company,
        read_company or crs.read_company,
        clock,
    )

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        yield
        if registry is not None:
            registry.close()

    app = FastAPI(title="poseidon-console", lifespan=lifespan)

    @app.exception_handler(PolicyDenied)
    async def denied(_: Request, exc: PolicyDenied) -> JSONResponse:
        return JSONResponse({"detail": str(exc), "rule": exc.rule}, status_code=403)

    @app.exception_handler(Invalid)
    async def invalid(_: Request, exc: Invalid) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=422)

    @app.exception_handler(NoCompany)
    async def no_company(_: Request, exc: NoCompany) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=404)

    @app.exception_handler(Unavailable)
    async def unavailable(_: Request, exc: Unavailable) -> JSONResponse:
        return JSONResponse({"detail": f"temporal unreachable: {exc}"}, status_code=503)

    def identity_of(request: Request):
        return request.state.identity

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    @app.get("/whoami")
    async def whoami(request: Request) -> dict[str, Any]:
        identity = identity_of(request)
        return {
            "human": identity.agent_id,
            "lifecycle": identity.lifecycle,
            "company": await service.applied(),
        }

    @app.post("/blueprint/preview")
    async def preview(body: BlueprintRequest) -> dict[str, Any]:
        return await service.preview(body.blueprint)

    @app.post("/blueprint/apply")
    async def apply(request: Request, body: BlueprintRequest) -> dict[str, Any]:
        return await service.apply(identity_of(request), body.blueprint)

    @app.get("/company")
    async def company() -> dict[str, Any]:
        return await service.company()

    @app.delete("/company")
    async def delete_company_route(
        request: Request, body: DeleteRequest
    ) -> dict[str, Any]:
        return await service.delete(identity_of(request), body.reason)

    @app.post("/runs")
    async def start_run(request: Request, body: StartRunRequest) -> dict[str, Any]:
        return await service.start_run(identity_of(request), body.workflow, body.number)

    @app.get("/runs")
    async def runs_since(since: str | None = None) -> list[dict[str, Any]]:
        return await service.runs_since(since)

    @app.get("/runs/{run_id}")
    async def run(run_id: str) -> dict[str, Any]:
        return await service.run(run_id)

    @app.get("/runs/{run_id}/story")
    async def story(run_id: str, decisions: bool = False) -> list[dict[str, Any]]:
        return await service.story(run_id, decisions)

    @app.get("/runs/{run_id}/spend")
    async def spend(run_id: str) -> dict[str, Any]:
        return await service.spend(run_id)

    @app.post("/runs/{run_id}/gates/{gate}")
    async def resolve_gate(
        request: Request, run_id: str, gate: str, body: VerdictRequest
    ) -> dict[str, Any]:
        return await service.resolve_gate(
            identity_of(request), run_id, gate, body.approved, body.reason
        )

    @app.post("/runs/{run_id}/cancel")
    async def cancel_run(
        request: Request, run_id: str, body: CancelRequest
    ) -> dict[str, Any]:
        return await service.cancel_run(identity_of(request), run_id, body.reason)

    @app.get("/heartbeat")
    async def heartbeat(since: str | None = None) -> dict[str, Any]:
        return await service.heartbeat(since)

    @app.post("/heartbeat/wake")
    async def wake(request: Request) -> dict[str, Any]:
        return await service.wake(identity_of(request))

    @app.get("/spend")
    async def spend_since(since: str | None = None) -> dict[str, Any]:
        return await service.spend_since(since)

    @app.get("/events")
    async def events(
        after: int = 0, run: str | None = None, limit: int = 200
    ) -> dict[str, Any]:
        return await service.events(after, run, max(1, min(limit, 1000)))

    @app.get("/now")
    async def now() -> dict[str, str]:
        return await service.now()

    app.add_middleware(
        AuthMiddleware, authenticator=authenticator, open_paths=("/healthz",)
    )
    app.state.service = service
    return app
