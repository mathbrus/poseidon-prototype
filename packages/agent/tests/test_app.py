"""The dispatch HTTP surface with an injected procedure runner."""

import asyncio

import httpx
import pytest

from agent.app import create_app
from agent.config import AgentSettings
from agent.procedures import ProcedureResult

pytestmark = pytest.mark.anyio

RUN_ID = "run-01TESTULID"


def client(app) -> httpx.AsyncClient:
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://agent.test"
    )


def settings_for(role: str) -> AgentSettings:
    return AgentSettings(
        gateway_token_file="/run/unused-token",
        mcp_gateway_url="http://gateway.test:8080",
        llm_proxy_url="http://llm.test:4000",
        model="test-model",
        role=role,
        target_repo="example/target",
        vision="ship a tiny SaaS under governance",
    )


def app_for(role: str, **runners):
    return create_app(settings=settings_for(role), runners=runners)


async def post(http: httpx.AsyncClient, procedure: str, task_text: str) -> httpx.Response:
    return await http.post(
        "/task", json={"run_id": RUN_ID, "procedure": procedure, "task_text": task_text}
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

    async def runner(run_id: str, task_text: str) -> ProcedureResult:
        seen.append((run_id, task_text))
        await release.wait()
        return ProcedureResult(pr_url="https://github.test/pull/1", iterations=2)

    async with client(app_for("implementer", implement=runner)) as http:
        resp = await post(http, "implement", "do it")
        assert resp.status_code == 202

        resp = await http.get(f"/task/{RUN_ID}")
        assert resp.json() == {
            "status": "running",
            "pr_url": None,
            "iterations": 0,
            "error": None,
            "ticket_urls": [],
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
        "comment_urls": [],
        "verdict": None,
        "verdict_summary": None,
        "decision": None,
    }
    assert seen == [(RUN_ID, "do it")]


async def test_procedure_result_error_maps_to_failed():
    async def runner(run_id: str, task_text: str) -> ProcedureResult:
        return ProcedureResult(
            iterations=3, error="tests still failing after 3 iterations"
        )

    async with client(app_for("implementer", implement=runner)) as http:
        await post(http, "implement", "do it")
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "failed"
    assert status["iterations"] == 3
    assert "still failing" in status["error"]


async def test_runner_crash_maps_to_failed():
    async def runner(run_id: str, task_text: str) -> ProcedureResult:
        raise RuntimeError("gateway unreachable")

    async with client(app_for("implementer", implement=runner)) as http:
        await post(http, "implement", "do it")
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "failed"
    assert status["error"] == "gateway unreachable"


async def test_unknown_run_is_404():
    async with client(app_for("implementer")) as http:
        resp = await http.get("/task/run-GHOST")
    assert resp.status_code == 404


async def test_duplicate_run_id_is_409():
    started = asyncio.Event()

    async def runner(run_id: str, task_text: str) -> ProcedureResult:
        await started.wait()
        return ProcedureResult(pr_url="https://pr", iterations=1)

    async with client(app_for("implementer", implement=runner)) as http:
        first = await post(http, "implement", "a")
        second = await post(http, "implement", "b")
        started.set()
        await poll_until_terminal(http, RUN_ID)

    assert first.status_code == 202
    assert second.status_code == 409


async def test_terminal_run_restarts_on_redispatch():
    """409 is for runs in flight (crash-resume re-attach); a terminal run
    re-dispatched restarts — the budget-pause resume path
    would otherwise poll the stale failure forever."""
    attempts: list[str] = []

    async def runner(run_id: str, task_text: str) -> ProcedureResult:
        attempts.append(task_text)
        if len(attempts) == 1:
            return ProcedureResult(error="budget_exhausted: cap reached")
        return ProcedureResult(pr_url="https://github.test/pull/2", iterations=1)

    async with client(app_for("implementer", implement=runner)) as http:
        first = await post(http, "implement", "one")
        assert first.status_code == 202
        status = await poll_until_terminal(http, RUN_ID)
        assert "budget_exhausted" in status["error"]

        again = await post(http, "implement", "two")
        assert again.status_code == 202
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "done"
    assert attempts == ["one", "two"]


async def test_running_run_still_answers_409():
    release = asyncio.Event()

    async def runner(run_id: str, task_text: str) -> ProcedureResult:
        await release.wait()
        return ProcedureResult()

    async with client(app_for("implementer", implement=runner)) as http:
        await post(http, "implement", "one")
        conflict = await post(http, "implement", "again")
        assert conflict.status_code == 409
        release.set()
        await poll_until_terminal(http, RUN_ID)


async def test_a_role_runs_only_its_own_procedures():
    """The pod's role bounds the dispatch: a PM host refuses implement, an
    implementer host refuses scope — 422 before any runner is touched. The
    gateway's tool policy is the boundary; this is the host declining to
    start a loop it could never finish."""

    async def never(run_id: str, task_text: str) -> ProcedureResult:
        raise AssertionError("a refused procedure must never run")

    async with client(app_for("pm", implement=never, decide=never)) as http:
        refused = await post(http, "implement", "write code")
        assert refused.status_code == 422
        assert "does not run the implement procedure" in refused.json()["detail"]
        assert (await http.get(f"/task/{RUN_ID}")).status_code == 404

    async with client(app_for("implementer", scope=never)) as http:
        assert (await post(http, "scope", "an observation")).status_code == 422

    async with client(app_for("no-such-role", implement=never)) as http:
        assert (await post(http, "implement", "x")).status_code == 422


async def test_a_dispatch_must_name_its_procedure():
    async with client(app_for("implementer")) as http:
        resp = await http.post("/task", json={"run_id": RUN_ID, "task_text": "do it"})
    assert resp.status_code == 422


async def test_review_verdict_flows_through_the_status_api():
    """A review dispatch's structured verdict must survive the HTTP
    boundary — the workflow reads it from the status response, and a
    dropped verdict fails every policy- and human-gated merge with
    'reviewer finished without a verdict'."""

    async def fake_review(run_id, task_text):
        return ProcedureResult(
            comment_urls=["https://pr/1#c-1"],
            verdict="pass",
            verdict_summary="Suite green; in scope.",
        )

    async with client(app_for("reviewer", review=fake_review)) as http:
        await post(http, "review", "review")
        status = await poll_until_terminal(http, RUN_ID)

    assert status["status"] == "done"
    assert status["comment_urls"] == ["https://pr/1#c-1"]
    assert status["verdict"] == "pass"
    assert status["verdict_summary"] == "Suite green; in scope."
