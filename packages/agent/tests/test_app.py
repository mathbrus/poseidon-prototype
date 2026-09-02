"""The dispatch HTTP surface with an injected task runner."""

import asyncio

import httpx
import pytest

from agent.app import create_app
from agent.brain import RunResult

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

    async with client(create_app(runners={"implement": runner})) as http:
        resp = await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
        assert resp.status_code == 202

        resp = await http.get(f"/task/{RUN_ID}")
        assert resp.json() == {
            "status": "running",
            "pr_url": None,
            "iterations": 0,
            "error": None,
            "ticket_urls": [],
            "crew_request_id": None,
            "comment_urls": [],
            "verdict": None,
            "verdict_summary": None,
            "decision": None,
        }

        release.set()
        status = await poll_until_terminal(http, RUN_ID)

    assert status == {
        "status": "done",
        "pr_url": "https://github.test/pull/1",
        "iterations": 2,
        "error": None,
        "ticket_urls": [],
        "crew_request_id": None,
        "comment_urls": [],
        "verdict": None,
        "verdict_summary": None,
        "decision": None,
    }
    assert seen == [(RUN_ID, "do it")]


async def test_run_result_error_maps_to_failed():
    async def runner(run_id: str, task_text: str) -> RunResult:
        return RunResult(iterations=3, error="tests still failing after 3 iterations")

    async with client(create_app(runners={"implement": runner})) as http:
        await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "failed"
    assert status["iterations"] == 3
    assert "still failing" in status["error"]


async def test_runner_crash_maps_to_failed():
    async def runner(run_id: str, task_text: str) -> RunResult:
        raise RuntimeError("gateway unreachable")

    async with client(create_app(runners={"implement": runner})) as http:
        await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "failed"
    assert status["error"] == "gateway unreachable"


async def test_unknown_run_is_404():
    async def runner(run_id: str, task_text: str) -> RunResult:
        return RunResult()

    async with client(create_app(runners={"implement": runner})) as http:
        resp = await http.get("/task/run-GHOST")
    assert resp.status_code == 404


async def test_duplicate_run_id_is_409():
    started = asyncio.Event()

    async def runner(run_id: str, task_text: str) -> RunResult:
        await started.wait()
        return RunResult(pr_url="https://pr", iterations=1)

    async with client(create_app(runners={"implement": runner})) as http:
        first = await http.post("/task", json={"run_id": RUN_ID, "task_text": "a"})
        second = await http.post("/task", json={"run_id": RUN_ID, "task_text": "b"})
        started.set()
        await poll_until_terminal(http, RUN_ID)

    assert first.status_code == 202
    assert second.status_code == 409


async def test_terminal_run_restarts_on_redispatch():
    """409 is for runs in flight (crash-resume re-attach); a terminal run
    re-dispatched restarts — the budget-pause resume path
    would otherwise poll the stale failure forever."""
    attempts: list[str] = []

    async def runner(run_id: str, task_text: str) -> RunResult:
        attempts.append(task_text)
        if len(attempts) == 1:
            return RunResult(error="budget_exhausted: cap reached")
        return RunResult(pr_url="https://github.test/pull/2", iterations=1)

    async with client(create_app(runners={"implement": runner})) as http:
        first = await http.post("/task", json={"run_id": RUN_ID, "task_text": "one"})
        assert first.status_code == 202
        status = await poll_until_terminal(http, RUN_ID)
        assert "budget_exhausted" in status["error"]

        again = await http.post("/task", json={"run_id": RUN_ID, "task_text": "two"})
        assert again.status_code == 202
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "done"
    assert attempts == ["one", "two"]


async def test_running_run_still_answers_409():
    release = asyncio.Event()

    async def runner(run_id: str, task_text: str) -> RunResult:
        await release.wait()
        return RunResult()

    async with client(create_app(runners={"implement": runner})) as http:
        await http.post("/task", json={"run_id": RUN_ID, "task_text": "one"})
        conflict = await http.post("/task", json={"run_id": RUN_ID, "task_text": "again"})
        assert conflict.status_code == 409
        release.set()
        await poll_until_terminal(http, RUN_ID)


async def test_review_verdict_flows_through_the_status_api():
    """A review dispatch's structured verdict must survive the HTTP
    boundary — the workflow reads it from the status response, and a
    dropped verdict fails every policy- and human-gated merge with
    'reviewer finished without a verdict'."""

    async def fake_review(run_id, task_text):
        return RunResult(
            comment_urls=["https://pr/1#c-1"],
            verdict="pass",
            verdict_summary="Suite green; in scope.",
        )

    async with client(create_app(runners={"review": fake_review})) as http:
        await http.post(
            "/task", json={"run_id": RUN_ID, "task_text": "review", "mode": "review"}
        )
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "done"
    assert status["comment_urls"] == ["https://pr/1#c-1"]
    assert status["verdict"] == "pass"
    assert status["verdict_summary"] == "Suite green; in scope."
