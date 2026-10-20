"""The agent's dispatch HTTP API.

POST /task {run_id, procedure, task_text, documents} -> 202, runs the
procedure in a background task (one concurrent run per host);
GET /task/{run_id} -> {status, pr_url, iterations, error, ...} — every
field of the `ProcedureResult`.

A dispatch names a procedure; the host runs it only if the pod's role
declares it (the catalog's `procedures`), refusing the rest with 422 before
any model call — defence in depth under the gateway's tool policy, not a
substitute for it. Every procedure acts under the pod's own projected
token.

Run with: uvicorn --factory agent.app:create_app
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent import procedures
from agent.config import AgentSettings
from agent.procedures import ProcedureResult
from common.telemetry import get_logger
from schema.catalog import ROLES

Procedure = Literal["scope", "decide", "implement", "deliver", "review"]
# One dispatch: the orchestrator asking this host to run a procedure on a
# text, with the company's documents beside it.
Dispatch = Callable[[str, str, dict[str, str]], Awaitable[ProcedureResult]]


class TaskRequest(BaseModel):
    run_id: str
    procedure: Procedure
    task_text: str
    # The company's documents by name, as the platform read them at
    # base_ref: untrusted context beside the task, empty for a procedure
    # that judges nothing.
    documents: dict[str, str] = {}


class TaskStatus(ProcedureResult):
    """The dispatch's HTTP shape: exactly the `ProcedureResult` plus a
    lifecycle `status`. Derived, not mirrored — every result field crosses
    the boundary automatically, so a new one can never be silently dropped."""

    status: Literal["running", "done", "failed"]


def create_app(
    settings: AgentSettings | None = None,
    dispatches: dict[str, Dispatch] | None = None,
) -> FastAPI:
    """`dispatches` overrides how any procedure is run — how tests drive
    the API without an SDK loop behind it."""

    def _settings() -> AgentSettings:
        # Lazy: the deployed app resolves env exactly once, on first
        # dispatch.
        nonlocal settings
        if settings is None:
            settings = AgentSettings()
        return settings

    # The procedures are synchronous (the SDK conversation loop is
    # blocking); each dispatch runs on a worker thread.
    def in_thread(
        run: Callable[[AgentSettings, str, str, dict[str, str]], ProcedureResult],
    ) -> Dispatch:
        async def dispatch(
            run_id: str, task_text: str, documents: dict[str, str]
        ) -> ProcedureResult:
            return await asyncio.to_thread(run, _settings(), run_id, task_text, documents)

        return dispatch

    dispatch_by_procedure: dict[str, Dispatch] = {
        "scope": in_thread(procedures.run_scope),
        "decide": in_thread(procedures.run_decide),
        "implement": in_thread(procedures.run_implement),
        "deliver": in_thread(procedures.run_deliver),
        "review": in_thread(procedures.run_review),
        **(dispatches or {}),
    }

    log = get_logger("agent")
    app = FastAPI(title="poseidon-agent")
    tasks: dict[str, TaskStatus] = {}
    background: set[asyncio.Task] = set()

    async def execute(request: TaskRequest) -> None:
        run_id = request.run_id
        run_log = log.bind(run_id=run_id, role=_settings().role)
        try:
            result = await dispatch_by_procedure[request.procedure](
                run_id, request.task_text, request.documents
            )
        except Exception as exc:
            run_log.exception("task crashed")
            tasks[run_id] = TaskStatus(status="failed", error=str(exc))
            return
        tasks[run_id] = TaskStatus(
            status="failed" if result.error else "done",
            **result.model_dump(),
        )
        run_log.info("task finished", extra=tasks[run_id].model_dump())

    @app.post("/task", status_code=202)
    async def submit_task(request: TaskRequest) -> TaskStatus:
        role = _settings().role
        allowed = ROLES[role].procedures if role in ROLES else ()
        if request.procedure not in allowed:
            raise HTTPException(
                status_code=422,
                detail=f"role {role!r} does not run the {request.procedure} procedure",
            )
        existing = tasks.get(request.run_id)
        if existing is not None and existing.status == "running":
            # Crash-resume semantics: the workflow's retry re-attaches to
            # the run already in flight.
            raise HTTPException(
                status_code=409, detail=f"run {request.run_id} already running"
            )
        # A terminal run re-dispatched is a restart — the budget-park
        # resume: the workflow re-dispatches after the cap
        # was raised, and polling the stale failure would loop forever.
        log.bind(run_id=request.run_id).info(
            "task accepted", extra={"procedure": request.procedure}
        )
        tasks[request.run_id] = TaskStatus(status="running")
        task = asyncio.create_task(execute(request))
        background.add(task)
        task.add_done_callback(background.discard)
        return tasks[request.run_id]

    @app.get("/task/{run_id}")
    async def task_status(run_id: str) -> TaskStatus:
        if run_id not in tasks:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return tasks[run_id]

    return app
