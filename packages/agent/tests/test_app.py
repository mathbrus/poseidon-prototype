"""S7 app tests: the C6 HTTP surface with an injected task runner."""

import asyncio

import httpx
import pytest

from agent.app import create_app
from agent.graph import RunResult

pytestmark = pytest.mark.anyio

RUN_ID = "run-01TESTULID"


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    )


async def poll_until_terminal(http: httpx.AsyncClient, run_id: str) -> dict:
    for _ in range(100):
        resp = await http.get(f"/task/{run_id}")
        assert resp.status_code == 200
        if resp.json()["status"] != "running":
            return resp.json()
        await asyncio.sleep(0.01)
    raise AssertionError("task never left 'running'")


async def test_task_lifecycle_running_then_done():
    release = asyncio.Event()
    seen: list[tuple[str, str]] = []

    async def runner(run_id: str, task_text: str) -> RunResult:
        seen.append((run_id, task_text))
        await release.wait()
        return RunResult(pr_url="https://github.test/pull/1", iterations=2)

    async with client(create_app(task_runner=runner)) as http:
        resp = await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
        assert resp.status_code == 202

        resp = await http.get(f"/task/{RUN_ID}")
        assert resp.json() == {
            "status": "running", "pr_url": None, "iterations": 0, "error": None,
        }

        release.set()
        status = await poll_until_terminal(http, RUN_ID)

    assert status == {
        "status": "done",
        "pr_url": "https://github.test/pull/1",
        "iterations": 2,
        "error": None,
    }
    assert seen == [(RUN_ID, "do it")]


async def test_run_result_error_maps_to_failed():
    async def runner(run_id: str, task_text: str) -> RunResult:
        return RunResult(iterations=3, error="tests still failing after 3 iterations")

    async with client(create_app(task_runner=runner)) as http:
        await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "failed"
    assert status["iterations"] == 3
    assert "still failing" in status["error"]


async def test_runner_crash_maps_to_failed():
    async def runner(run_id: str, task_text: str) -> RunResult:
        raise RuntimeError("gateway unreachable")

    async with client(create_app(task_runner=runner)) as http:
        await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "failed"
    assert status["error"] == "gateway unreachable"


async def test_unknown_run_is_404():
    async def runner(run_id: str, task_text: str) -> RunResult:
        return RunResult()

    async with client(create_app(task_runner=runner)) as http:
        resp = await http.get("/task/run-GHOST")
    assert resp.status_code == 404


async def test_duplicate_run_id_is_409():
    started = asyncio.Event()

    async def runner(run_id: str, task_text: str) -> RunResult:
        await started.wait()
        return RunResult(pr_url="https://pr", iterations=1)

    async with client(create_app(task_runner=runner)) as http:
        first = await http.post("/task", json={"run_id": RUN_ID, "task_text": "a"})
        second = await http.post("/task", json={"run_id": RUN_ID, "task_text": "b"})
        started.set()
        await poll_until_terminal(http, RUN_ID)

    assert first.status_code == 202
    assert second.status_code == 409
