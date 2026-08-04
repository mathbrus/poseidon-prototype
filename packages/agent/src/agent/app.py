"""Agent HTTP API — contract C6.

POST /task {run_id, task_text} -> 202, runs the graph in a background task
(one concurrent run is all the PoC needs); GET /task/{run_id} ->
{status, pr_url, iterations, error}.

Run with: uvicorn --factory agent.app:create_app
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent.config import AgentSettings
from agent.graph import RunResult, run_task
from common.telemetry import get_logger

TaskRunner = Callable[[str, str], Awaitable[RunResult]]


class TaskRequest(BaseModel):
    run_id: str
    task_text: str


class TaskStatus(BaseModel):
    status: Literal["running", "done", "failed"]
    pr_url: str | None = None
    iterations: int = 0
    error: str | None = None


def create_app(
    settings: AgentSettings | None = None, task_runner: TaskRunner | None = None
) -> FastAPI:
    if task_runner is None:
        agent_settings = settings or AgentSettings()

        async def task_runner(run_id: str, task_text: str) -> RunResult:
            return await run_task(agent_settings, run_id, task_text)

    log = get_logger("agent", agent_id="backend-engineer-1")
    app = FastAPI(title="poseidon-agent")
    tasks: dict[str, TaskStatus] = {}
    background: set[asyncio.Task] = set()

    async def execute(run_id: str, task_text: str) -> None:
        run_log = log.bind(run_id=run_id)
        try:
            result = await task_runner(run_id, task_text)
        except Exception as exc:
            run_log.exception("task crashed")
            tasks[run_id] = TaskStatus(status="failed", error=str(exc))
            return
        tasks[run_id] = TaskStatus(
            status="failed" if result.error else "done",
            pr_url=result.pr_url,
            iterations=result.iterations,
            error=result.error,
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
        log.bind(run_id=request.run_id).info("task accepted")
        tasks[request.run_id] = TaskStatus(status="running")
        task = asyncio.create_task(execute(request.run_id, request.task_text))
        background.add(task)
        task.add_done_callback(background.discard)
        return tasks[request.run_id]

    @app.get("/task/{run_id}")
    async def task_status(run_id: str) -> TaskStatus:
        if run_id not in tasks:
            raise HTTPException(status_code=404, detail=f"unknown run {run_id}")
        return tasks[run_id]

    return app
