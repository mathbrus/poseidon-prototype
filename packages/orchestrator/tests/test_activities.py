"""Unit tests for the implement activities: step behavior and retry safety
against fakes. Sequencing, compensation and durability are workflow
concerns, proven live by the kind E1 suite (worker-kill included)."""

import asyncio
from pathlib import Path
from typing import Any

import pytest

from orchestrator.activities.implement import ImplementActivities
from orchestrator.config import WorkerSettings
from orchestrator.manifest import read_manifest
from sandbox.contract import SandboxHandle

RUN_ID = "run-test-0001"


class FakeBackend:
    def __init__(self) -> None:
        self.provisioned: list[str] = []
        self.destroyed: list[str] = []
        self.destroy_error: Exception | None = None

    def provision(self, run_id: str, workspace_tar: bytes) -> SandboxHandle:
        self.provisioned.append(run_id)
        return SandboxHandle(
            run_id=run_id, container_id="cid-1", url="http://10.0.0.9:8000"
        )

    def destroy(self, handle: SandboxHandle) -> None:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(handle.container_id)


@pytest.fixture
def settings(tmp_path: Path, monkeypatch) -> WorkerSettings:
    return WorkerSettings(
        orchestrator_bearer_token="orchestrator-token",
        mcp_gateway_url="http://gateway.test:8080",
        agent_url="http://agent.test:8000",
        target_repo="github.com/example/repo",
        runs_dir=tmp_path / "runs",
        poll_interval_s=0.01,
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def activities(settings: WorkerSettings, backend: FakeBackend) -> ImplementActivities:
    return ImplementActivities(settings, backend)


def test_create_run_writes_running_manifest(activities, settings):
    asyncio.run(activities.create_run(RUN_ID, "do the thing"))
    manifest = read_manifest(settings.runs_dir, RUN_ID)
    assert manifest.status == "running"
    assert manifest.task == "do the thing"
    assert manifest.finished_at is None


def test_create_run_is_retry_safe(activities, settings):
    asyncio.run(activities.create_run(RUN_ID, "attempt one"))
    asyncio.run(activities.create_run(RUN_ID, "attempt two"))
    assert read_manifest(settings.runs_dir, RUN_ID).task == "attempt two"


def test_teardown_destroys_and_finalizes_done(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    handle = {"run_id": RUN_ID, "container_id": "cid-1", "url": "http://x:8000"}
    result = {"status": "done", "pr_url": "https://pr", "iterations": 3, "error": None}
    record = asyncio.run(activities.teardown(RUN_ID, handle, result, None))
    assert backend.destroyed == ["cid-1"]
    assert record["status"] == "done"
    assert record["pr_url"] == "https://pr"
    assert record["iterations"] == 3
    manifest = read_manifest(settings.runs_dir, RUN_ID)
    assert manifest.status == "done"
    assert manifest.finished_at is not None


def test_teardown_records_error_and_still_destroys(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    handle = {"run_id": RUN_ID, "container_id": "cid-1", "url": "http://x:8000"}
    record = asyncio.run(activities.teardown(RUN_ID, handle, None, "dispatch_work: boom"))
    assert backend.destroyed == ["cid-1"]
    assert record["status"] == "failed"
    assert record["error"] == "dispatch_work: boom"


def test_teardown_without_sandbox_finalizes_failed(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "fetch_workspace: 500"))
    assert backend.destroyed == []
    assert record["status"] == "failed"


def test_teardown_surfaces_destroy_failure_as_error(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    backend.destroy_error = RuntimeError("daemon gone")
    handle = {"run_id": RUN_ID, "container_id": "cid-1", "url": "http://x:8000"}
    record = asyncio.run(activities.teardown(RUN_ID, handle, {"status": "done"}, None))
    assert record["status"] == "failed"
    assert "daemon gone" in record["error"]


def test_teardown_reconstructs_missing_manifest(activities, settings, backend):
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "lost volume"))
    assert record["status"] == "failed"
    assert record["run_id"] == RUN_ID


class FakeAgentTransport:
    """Drives AgentClient at the httpx layer via a MockTransport-like app."""

    def __init__(self, dispatch_codes: list[int], statuses: list[dict[str, Any]]):
        self.dispatch_codes = dispatch_codes
        self.statuses = statuses
        self.dispatches = 0

    def handler(self, request):
        import httpx

        if request.method == "POST" and request.url.path == "/task":
            code = self.dispatch_codes[min(self.dispatches, len(self.dispatch_codes) - 1)]
            self.dispatches += 1
            return httpx.Response(code, json={})
        if request.method == "GET" and request.url.path.startswith("/task/"):
            status = self.statuses.pop(0) if len(self.statuses) > 1 else self.statuses[0]
            if status.get("__code") == 404:
                return httpx.Response(404, json={"detail": "unknown run"})
            return httpx.Response(200, json=status)
        return httpx.Response(404)


def _patch_agent(monkeypatch, transport: FakeAgentTransport) -> None:
    import httpx

    from orchestrator import clients

    original_init = clients.AgentClient.__init__

    def patched(self, url: str) -> None:
        original_init(self, url)
        self._http = httpx.AsyncClient(
            base_url=url, transport=httpx.MockTransport(transport.handler)
        )

    monkeypatch.setattr(clients.AgentClient, "__init__", patched)


def test_dispatch_work_polls_to_terminal(activities, monkeypatch):
    transport = FakeAgentTransport(
        dispatch_codes=[202],
        statuses=[
            {"status": "running"},
            {"status": "done", "pr_url": "https://pr", "iterations": 2, "error": None},
        ],
    )
    _patch_agent(monkeypatch, transport)
    status = asyncio.run(activities.dispatch_work(RUN_ID, "task"))
    assert status["status"] == "done"
    assert status["pr_url"] == "https://pr"


def test_dispatch_work_tolerates_conflict_and_reattaches(activities, monkeypatch):
    """A retry after worker death re-dispatches, gets 409, and polls the
    already-running agent to completion."""
    transport = FakeAgentTransport(
        dispatch_codes=[409],
        statuses=[{"status": "done", "pr_url": None, "iterations": 1, "error": "red"}],
    )
    _patch_agent(monkeypatch, transport)
    status = asyncio.run(activities.dispatch_work(RUN_ID, "task"))
    assert transport.dispatches == 1
    assert status["error"] == "red"


def test_dispatch_work_raises_when_agent_lost_the_run(activities, monkeypatch):
    from orchestrator.clients import AgentRunLost

    transport = FakeAgentTransport(
        dispatch_codes=[409], statuses=[{"__code": 404}]
    )
    _patch_agent(monkeypatch, transport)
    with pytest.raises(AgentRunLost):
        asyncio.run(activities.dispatch_work(RUN_ID, "task"))
