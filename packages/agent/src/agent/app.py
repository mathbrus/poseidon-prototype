"""Agent HTTP API — contract C6.

POST /task {run_id, task_text, mode?, gateway_token?} -> 202, runs the
graph in a background task (one concurrent run is all the PoC needs);
GET /task/{run_id} -> {status, pr_url, iterations, error, brief}.

Mode `plan` (plan step 16d) runs the read-only planner and finishes with
`brief`; the optional per-dispatch `gateway_token` is how the planner acts
under its own minted identity while borrowing this brain host (§4.4 —
the planner gets no pod of its own).

Run with: uvicorn --factory agent.app:create_app
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent.config import AgentSettings
from agent.graph import RunResult, run_deliver, run_plan, run_pm, run_review, run_task
from common.telemetry import get_logger

TaskRunner = Callable[[str, str], Awaitable[RunResult]]


class TaskRequest(BaseModel):
    run_id: str
    task_text: str
    mode: Literal["implement", "plan", "deliver", "review"] = "implement"
    # The dispatching workflow may hand this task its own identity — the
    # planner's minted token when the planner borrows the executor's pod.
    gateway_token: str | None = None


class TaskStatus(BaseModel):
    status: Literal["running", "done", "failed"]
    pr_url: str | None = None
    iterations: int = 0
    error: str | None = None
    brief: str | None = None
    ticket_urls: list[str] = []
    crew_request_id: str | None = None
    comment_urls: list[str] = []


def create_app(
    settings: AgentSettings | None = None,
    task_runner: TaskRunner | None = None,
    plan_runner: Callable[..., Awaitable[RunResult]] | None = None,
) -> FastAPI:
    def _settings() -> AgentSettings:
        # Lazy: tests inject runners and configure no environment; the
        # deployed app resolves env exactly once, on first dispatch.
        nonlocal settings
        if settings is None:
            settings = AgentSettings()
        return settings

    if task_runner is None:
        async def task_runner(run_id: str, task_text: str) -> RunResult:
            # Role-template-driven (plan step 17): the PM instance scopes
            # and delegates; every other template implements.
            if _settings().role_template == "pm":
                return await run_pm(_settings(), run_id, task_text)
            return await run_task(_settings(), run_id, task_text)

    if plan_runner is None:
        async def plan_runner(
            run_id: str, task_text: str, gateway_token: str | None
        ) -> RunResult:
            return await run_plan(_settings(), run_id, task_text, gateway_token)

    async def deliver_runner(run_id: str, task_text: str) -> RunResult:
        return await run_deliver(_settings(), run_id, task_text)

    async def review_runner(run_id: str, task_text: str) -> RunResult:
        return await run_review(_settings(), run_id, task_text)

    log = get_logger("agent", agent_id="backend-engineer-1")
    app = FastAPI(title="poseidon-agent")
    tasks: dict[str, TaskStatus] = {}
    background: set[asyncio.Task] = set()

    async def execute(request: TaskRequest) -> None:
        run_id = request.run_id
        run_log = log.bind(run_id=run_id)
        try:
            if request.mode == "plan":
                result = await plan_runner(
                    run_id, request.task_text, request.gateway_token
                )
            elif request.mode == "deliver":
                result = await deliver_runner(run_id, request.task_text)
            elif request.mode == "review":
                result = await review_runner(run_id, request.task_text)
            else:
                result = await task_runner(run_id, request.task_text)
        except Exception as exc:
            run_log.exception("task crashed")
            tasks[run_id] = TaskStatus(status="failed", error=str(exc))
            return
        tasks[run_id] = TaskStatus(
            status="failed" if result.error else "done",
            pr_url=result.pr_url,
            iterations=result.iterations,
            error=result.error,
            brief=result.brief,
            ticket_urls=result.ticket_urls,
            crew_request_id=result.crew_request_id,
            comment_urls=result.comment_urls,
        )
        run_log.info("task finished", extra=tasks[run_id].model_dump())

    @app.post("/task", status_code=202)
    async def submit_task(request: TaskRequest) -> TaskStatus:
        existing = tasks.get(request.run_id)
        if existing is not None and existing.status == "running":
            # Crash-resume semantics: the workflow's retry re-attaches to
            # the run already in flight.
            raise HTTPException(status_code=409, detail=f"run {request.run_id} already running")
        # A terminal run re-dispatched is a restart — the budget-pause
        # resume (plan step 14): the workflow re-dispatches after the cap
        # was raised, and polling the stale failure would loop forever.
        log.bind(run_id=request.run_id).info(
            "task accepted", extra={"mode": request.mode}
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
