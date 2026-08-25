"""The agent's dispatch HTTP API.

POST /task {run_id, task_text, mode?} -> 202, runs the
dispatch in a background task (one concurrent run per host);
GET /task/{run_id} -> {status, pr_url, iterations, error, brief}.

Mode `plan` runs the read-only planner and finishes with
`brief`; like every mode it acts under the pod's own projected token —
each crew member runs on its own brain host.

Run with: uvicorn --factory agent.app:create_app
"""

import asyncio
from collections.abc import Awaitable, Callable
from typing import Literal

from fastapi import FastAPI, HTTPException
from pydantic import BaseModel

from agent import brain
from agent.brain import RunResult
from agent.config import AgentSettings
from common.telemetry import get_logger

Mode = Literal["implement", "plan", "deliver", "review", "decide"]
Runner = Callable[[str, str], Awaitable[RunResult]]


class TaskRequest(BaseModel):
    run_id: str
    task_text: str
    mode: Mode = "implement"


class TaskStatus(RunResult):
    """The dispatch's HTTP shape: exactly the brain's `RunResult` plus a
    lifecycle `status`. Derived, not mirrored — every result field crosses
    the boundary automatically, so a new one can never be silently dropped."""

    status: Literal["running", "done", "failed"]


def create_app(
    settings: AgentSettings | None = None,
    runners: dict[str, Runner] | None = None,
) -> FastAPI:
    """`runners` overrides the brain's runner for any mode — how tests drive
    the API without an SDK loop behind it."""

    def _settings() -> AgentSettings:
        # Lazy: tests inject runners and configure no environment; the
        # deployed app resolves env exactly once, on first dispatch.
        nonlocal settings
        if settings is None:
            settings = AgentSettings()
        return settings

    # The brain's mode runners are synchronous (the SDK conversation loop
    # is blocking); each dispatch runs on a worker thread.
    def in_thread(dispatch: Callable[[AgentSettings, str, str], RunResult]) -> Runner:
        async def run(run_id: str, task_text: str) -> RunResult:
            return await asyncio.to_thread(dispatch, _settings(), run_id, task_text)

        return run

    async def implement(run_id: str, task_text: str) -> RunResult:
        # Role-template-driven: the PM instance scopes and delegates; every
        # other template implements.
        dispatch = (
            brain.pm_dispatch
            if _settings().role_template == "pm"
            else brain.implement_dispatch
        )
        return await asyncio.to_thread(dispatch, _settings(), run_id, task_text)

    modes: dict[str, Runner] = {
        "implement": implement,
        "plan": in_thread(brain.plan_dispatch),
        "deliver": in_thread(brain.deliver_dispatch),
        "review": in_thread(brain.review_dispatch),
        "decide": in_thread(brain.decide_dispatch),
        **(runners or {}),
    }

    log = get_logger("agent", agent_id="backend-engineer-1")
    app = FastAPI(title="poseidon-agent")
    tasks: dict[str, TaskStatus] = {}
    background: set[asyncio.Task] = set()

    async def execute(request: TaskRequest) -> None:
        run_id = request.run_id
        run_log = log.bind(run_id=run_id)
        try:
            result = await modes[request.mode](run_id, request.task_text)
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
        existing = tasks.get(request.run_id)
        if existing is not None and existing.status == "running":
            # Crash-resume semantics: the workflow's retry re-attaches to
            # the run already in flight.
            raise HTTPException(
                status_code=409, detail=f"run {request.run_id} already running"
            )
        # A terminal run re-dispatched is a restart — the budget-pause
        # resume: the workflow re-dispatches after the cap
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
