"""The console's HTTP surface.

JSON in, JSON out, a bearer token on every request but the health probe.
Every route is one entry of `common.console.ROUTES`, the console
interface, served with that entry's request and response models, and
every refusal is one of the interface's two error bodies: a `Problem`
(401 no verified human, 404 no company applied, 422 something that
cannot be acted on, 503 Temporal unreachable) or, for an act policy
refuses, a 403 `PolicyRefusal` naming the rule.

Run with: uvicorn --factory console.app:create_app
"""

from collections.abc import Callable
from contextlib import asynccontextmanager
from typing import Any, Literal

from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from common.authn import Authenticator, AuthMiddleware, Reviewer, default_reviewer
from common.console import (
    BlueprintRequest,
    CancelRequest,
    DeleteRequest,
    Gate,
    Health,
    PauseRequest,
    PolicyRefusal,
    Problem,
    ResumeRequest,
    StartRunRequest,
    VerdictRequest,
    WhoAmI,
    route,
)
from common.topology import CONSOLE_TOKEN_AUDIENCE
from common.version import running_version
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
    FiledTickets,
    RunParticipants,
    Runs,
    open_registry,
)

# The error bodies every route may answer, and the one only an act may.
READ_ERRORS: dict[int | str, dict[str, Any]] = {
    status: {"model": Problem} for status in (401, 404, 422, 503)
}
ACT_ERRORS = {**READ_ERRORS, 403: {"model": PolicyRefusal}}


def _problem(status: int, detail: str) -> JSONResponse:
    return JSONResponse(Problem(detail=detail).model_dump(), status_code=status)


def _validation_detail(exc: RequestValidationError) -> str:
    """FastAPI's list of errors as one sentence: where, and what."""
    return "; ".join(
        f"{'.'.join(str(part) for part in error['loc'])}: {error['msg']}"
        for error in exc.errors()
    )


def create_app(
    settings: ConsoleSettings | None = None,
    audit: AuditStore | None = None,
    runs: Runs | None = None,
    ledger: BudgetLedger | None = None,
    instances: AgentInstances | None = None,
    companies: Companies | None = None,
    participants: RunParticipants | None = None,
    tickets: FiledTickets | None = None,
    token_reviewer: Reviewer | None = None,
    workflows: Workflows | None = None,
    submit_crs: Callable[[DeploymentPlan], tuple[int, int, int]] | None = None,
    delete_company: Callable[[str, bool], None] | None = None,
    read_company: Callable[[], dict[str, Any] | None] | None = None,
    clock: Callable[[], str] | None = None,
) -> FastAPI:
    settings = settings or ConsoleSettings()
    accessors = (audit, runs, ledger, instances, companies, participants, tickets)
    registry = None
    if all(a is None for a in accessors):
        registry = open_registry()
        audit = AuditStore(registry)
        runs = Runs(registry)
        ledger = BudgetLedger(registry)
        instances = AgentInstances(registry)
        companies = Companies(registry)
        participants = RunParticipants(registry)
        tickets = FiledTickets(registry)
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
        participants,
        tickets,
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

    app = FastAPI(
        title="poseidon-console",
        description="The console interface: the one way a person acts on a company"
        " and reads it. Every act is decided by policy and audited under the"
        " human's name.",
        version=running_version(),
        lifespan=lifespan,
    )

    def serve(method: str, path: str):
        """Register a handler for one route of the interface, with its
        response model and the errors it may answer."""
        entry = route(method, path)
        return app.api_route(
            path,
            methods=[method],
            response_model=entry.response,
            responses=ACT_ERRORS if entry.decision else READ_ERRORS,
            summary=entry.summary,
        )

    @app.exception_handler(PolicyDenied)
    async def denied(_: Request, exc: PolicyDenied) -> JSONResponse:
        refusal = PolicyRefusal(detail=str(exc), rule=exc.rule)
        return JSONResponse(refusal.model_dump(), status_code=403)

    @app.exception_handler(Invalid)
    async def invalid(_: Request, exc: Invalid) -> JSONResponse:
        return _problem(422, str(exc))

    @app.exception_handler(RequestValidationError)
    async def malformed(_: Request, exc: RequestValidationError) -> JSONResponse:
        return _problem(422, _validation_detail(exc))

    @app.exception_handler(NoCompany)
    async def no_company(_: Request, exc: NoCompany) -> JSONResponse:
        return _problem(404, str(exc))

    @app.exception_handler(Unavailable)
    async def unavailable(_: Request, exc: Unavailable) -> JSONResponse:
        return _problem(503, f"temporal unreachable: {exc}")

    def identity_of(request: Request):
        return request.state.identity

    @serve("GET", "/healthz")
    async def healthz():
        return Health(ok=True, version=running_version())

    @serve("GET", "/whoami")
    async def whoami(request: Request):
        identity = identity_of(request)
        return WhoAmI(
            human=identity.agent_id,
            lifecycle=identity.lifecycle,
            company=await service.applied(),
        )

    @serve("POST", "/blueprint/preview")
    async def preview(body: BlueprintRequest):
        return await service.preview(body.blueprint)

    @serve("POST", "/blueprint/apply")
    async def apply(request: Request, body: BlueprintRequest):
        return await service.apply(identity_of(request), body.blueprint)

    @serve("GET", "/company")
    async def company():
        return await service.company()

    @serve("GET", "/company/bounds")
    async def bounds():
        return await service.bounds()

    @serve("GET", "/agents")
    async def agents():
        return await service.agents()

    @serve("GET", "/tickets")
    async def tickets_filed():
        return await service.tickets()

    @serve("DELETE", "/company")
    async def delete_company_route(request: Request, body: DeleteRequest):
        return await service.delete(identity_of(request), body.rationale, body.wait)

    @serve("POST", "/runs")
    async def start_run(request: Request, body: StartRunRequest):
        return await service.start_run(
            identity_of(request), body.workflow.value, body.number
        )

    @serve("GET", "/runs")
    async def runs_since(
        since: str | None = None, state: Literal["parked"] | None = None
    ):
        return await service.runs_since(since, parked_only=state == "parked")

    @serve("GET", "/runs/{run_id}")
    async def run(run_id: str):
        return await service.run(run_id)

    @serve("GET", "/runs/{run_id}/story")
    async def story(run_id: str, decisions: bool = False):
        return await service.story(run_id, decisions)

    @serve("GET", "/runs/{run_id}/spend")
    async def spend(run_id: str):
        return await service.spend(run_id)

    @serve("GET", "/runs/{run_id}/crew")
    async def crew(run_id: str):
        return await service.crew(run_id)

    @serve("POST", "/runs/{run_id}/gates/{gate}")
    async def resolve_gate(
        request: Request, run_id: str, gate: Gate, body: VerdictRequest
    ):
        return await service.resolve_gate(
            identity_of(request), run_id, gate.value, body.approved, body.rationale
        )

    @serve("POST", "/runs/{run_id}/cancel")
    async def cancel_run(request: Request, run_id: str, body: CancelRequest):
        return await service.cancel_run(identity_of(request), run_id, body.rationale)

    @serve("GET", "/heartbeat")
    async def heartbeat(since: str | None = None):
        return await service.heartbeat(since)

    @serve("POST", "/heartbeat/wake")
    async def wake(request: Request):
        return await service.wake(identity_of(request))

    @serve("POST", "/heartbeat/pause")
    async def pause(request: Request, body: PauseRequest):
        return await service.pause(identity_of(request), body.rationale)

    @serve("POST", "/heartbeat/resume")
    async def resume(request: Request, body: ResumeRequest):
        return await service.resume(identity_of(request), body.rationale)

    @serve("GET", "/spend")
    async def spend_since(since: str | None = None):
        return await service.spend_since(since)

    @serve("GET", "/events")
    async def events(after: int = 0, run: str | None = None, limit: int = 200):
        return await service.events(after, run, max(1, min(limit, 1000)))

    @serve("GET", "/now")
    async def now():
        return await service.now()

    app.add_middleware(
        AuthMiddleware, authenticator=authenticator, open_paths=("/healthz",)
    )
    app.state.service = service
    return app
