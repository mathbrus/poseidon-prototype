"""Unit tests for the implement activities: step behavior and retry safety
against fakes. Sequencing, compensation and durability are workflow
concerns, proven live by the kind E1 suite (worker-kill included)."""

import asyncio
from typing import Any

import pytest

from orchestrator.activities.implement import ImplementActivities
from orchestrator.config import WorkerSettings
from registry.memory import InMemoryCompanies, InMemoryRuns
from sandbox.contract import SandboxHandle, SandboxPolicy

RUN_ID = "run-test-0001"

# What an applied hello-saas blueprint records (the Company CR spec's infra
# section) — the SandboxPolicy source since plan step 12.
APPLIED_SPEC = {
    "namespace": "hello-saas",
    "infra": {
        "sandbox": {
            "image": "poseidon-sandbox",
            "workspace_dir": "/workspace",
            "cpu": "1",
            "memory": "1Gi",
            "disk": "2Gi",
            "deadline_s": 3600,
            "egress_allow": ["pypi.org"],
        }
    },
}


class FakeBackend:
    def __init__(self) -> None:
        self.provisioned: list[tuple[str, SandboxPolicy]] = []
        self.destroyed: list[str] = []
        self.destroy_error: Exception | None = None

    def provision(
        self, run_id: str, workspace_tar: bytes, policy: SandboxPolicy
    ) -> SandboxHandle:
        self.provisioned.append((run_id, policy))
        return SandboxHandle(
            run_id=run_id,
            url="http://192.168.0.9:8000",
            namespace="hello-saas",
            job_name="poseidon-sbx-run-test-0001",
            pod_name="poseidon-sbx-run-test-0001-abcde",
        )

    def destroy(self, handle: SandboxHandle) -> None:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(handle.job_name)


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        agent_url="http://agent.test:8000",
        target_repo="github.com/example/repo",
        sandbox_image="poseidon-sandbox:abc123",
        poll_interval_s=0.01,
    )


@pytest.fixture
def backend() -> FakeBackend:
    return FakeBackend()


@pytest.fixture
def runs() -> InMemoryRuns:
    return InMemoryRuns()


@pytest.fixture
def companies() -> InMemoryCompanies:
    store = InMemoryCompanies()
    store.upsert("hello-saas", APPLIED_SPEC)
    return store


@pytest.fixture
def activities(
    settings: WorkerSettings,
    backend: FakeBackend,
    runs: InMemoryRuns,
    companies: InMemoryCompanies,
) -> ImplementActivities:
    return ImplementActivities(settings, backend, runs, companies)


def test_create_run_opens_a_running_row(activities, runs):
    asyncio.run(activities.create_run(RUN_ID, "do the thing"))
    record = runs.get(RUN_ID)
    assert record.status == "running"
    assert record.task == "do the thing"
    assert record.finished_at is None
    # §5.5: the workflow id derives from the run id and is recorded with it.
    assert record.workflow_id == f"implement-{RUN_ID}"


def test_create_run_is_retry_safe(activities, runs):
    asyncio.run(activities.create_run(RUN_ID, "attempt one"))
    asyncio.run(activities.create_run(RUN_ID, "attempt two"))
    assert runs.get(RUN_ID).task == "attempt two"


JOB_HANDLE = {
    "run_id": RUN_ID,
    "url": "http://192.168.0.9:8000",
    "namespace": "hello-saas",
    "job_name": "poseidon-sbx-run-test-0001",
    "pod_name": "poseidon-sbx-run-test-0001-abcde",
}


def test_sandbox_policy_comes_from_the_applied_blueprint(activities):
    """Plan step 12 (sequencing choice 2 closed): the infra IR in the
    registry is the SandboxPolicy source; the logical default image resolves
    to the deploy-time reference the cluster actually carries."""
    policy = activities._sandbox_policy(RUN_ID)
    assert policy.image == "poseidon-sandbox:abc123"
    assert policy.deadline_s == 3600
    assert policy.egress_allow == ["pypi.org"]
    assert policy.env == {}  # no proxy configured -> nothing injected


def test_custom_toolchain_image_passes_through(activities, companies):
    spec = dict(APPLIED_SPEC)
    spec["infra"] = {"sandbox": {**APPLIED_SPEC["infra"]["sandbox"],
                                 "image": "python:3.12-slim"}}
    companies.upsert("hello-saas", spec)
    assert activities._sandbox_policy(RUN_ID).image == "python:3.12-slim"


def test_provision_without_an_applied_blueprint_fails_closed(
    settings, backend, runs
):
    lone = ImplementActivities(settings, backend, runs, InMemoryCompanies())
    with pytest.raises(RuntimeError, match="no applied blueprint"):
        lone._sandbox_policy(RUN_ID)
    assert backend.provisioned == []


def test_sandbox_policy_injects_per_run_proxy_credentials(activities, settings):
    settings.egress_proxy_url = "http://egress-proxy.platform:3128"
    settings.egress_proxy_key = "test-key"
    env = activities._sandbox_policy(RUN_ID).env
    assert env["HTTPS_PROXY"].startswith(f"http://{RUN_ID}:")
    assert env["HTTPS_PROXY"].endswith("@egress-proxy.platform:3128")
    # Distinct runs get distinct credentials — that is the attribution.
    other = activities._sandbox_policy("run-other").env
    assert other["HTTPS_PROXY"] != env["HTTPS_PROXY"]


def test_create_run_records_the_company(activities, runs):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    assert runs.get(RUN_ID).company == "hello-saas"


def test_teardown_destroys_and_finalizes_done(activities, settings, backend, runs):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    result = {"status": "done", "pr_url": "https://pr", "iterations": 3, "error": None}
    record = asyncio.run(activities.teardown(RUN_ID, dict(JOB_HANDLE), result, None))
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert record["status"] == "done"
    assert record["pr_url"] == "https://pr"
    assert record["iterations"] == 3
    stored = runs.get(RUN_ID)
    assert stored.status == "done"
    assert stored.finished_at is not None


def test_teardown_records_error_and_still_destroys(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), None, "dispatch_work: boom")
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert record["status"] == "failed"
    assert record["error"] == "dispatch_work: boom"


def test_teardown_without_sandbox_finalizes_failed(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "fetch_workspace: 500"))
    assert backend.destroyed == []
    assert record["status"] == "failed"


def test_teardown_surfaces_destroy_failure_as_error(activities, settings, backend):
    asyncio.run(activities.create_run(RUN_ID, "task"))
    backend.destroy_error = RuntimeError("apiserver gone")
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), {"status": "done"}, None)
    )
    assert record["status"] == "failed"
    assert "apiserver gone" in record["error"]


def test_teardown_reconstructs_a_missing_run_row(activities, settings, backend):
    """A fresh database mid-run must not break compensation."""
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "row vanished"))
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
