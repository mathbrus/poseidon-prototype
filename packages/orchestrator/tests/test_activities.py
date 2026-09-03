"""Unit tests for the implement activities — the steps between a run's
birth and its teardown — against fakes: the ticket read, sandbox policy
from the applied blueprint, the agent dispatch, and the verify gate.
Sequencing, compensation and durability are workflow concerns, proven
live by the kind e2e suite (worker-kill included)."""

import asyncio
from typing import Any

import pytest

from common.tasks import issue_run_task
from orchestrator.activities.implement import ImplementActivities
from orchestrator.config import WorkerSettings
from registry.memory import (
    InMemoryCompanies,
    InMemoryRuns,
)
from sandbox.contract import SandboxHandle, SandboxPolicy

RUN_ID = "run-test-0001"

# What an applied hello-saas blueprint records (the Company CR spec's infra
# section) — the SandboxPolicy source.
APPLIED_SPEC = {
    "namespace": "hello-saas",
    "repo": {"url": "github.com/example/repo", "base_ref": "main"},
    "mission": "ship it",
    "infra": {
        "sandbox": {
            "image": "poseidon-sandbox",
            "workspace_dir": "/workspace",
            "cpu": "1",
            "memory": "1Gi",
            "disk": "2Gi",
            "deadline_s": 3600,
            "test_command": "pytest -q",
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


def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected-token")
    return WorkerSettings(
        opa_url="http://opa.test:8181",
        platform_namespace="poseidon-platform",
        agent_image="poseidon-agent:abc123",
        model="mock-brain",
        egress_proxy_url="http://127.0.0.1:3128",
        egress_proxy_key="egress-key",
        timeout_profile="default",
        company="hello-saas",
        temporal_address="temporal.test:7233",
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        sandbox_image="poseidon-sandbox:abc123",
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


def _issue_gateway(monkeypatch, fake_gateway, issue: dict[str, Any]):
    import orchestrator.activities.implement as module

    async def read_issue(self, run_id, repo, number):
        return issue

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(read_issue=read_issue))


def test_read_issue_returns_the_view_and_links_the_ticket(
    activities, runs, monkeypatch, fake_gateway
):
    _issue_gateway(
        monkeypatch,
        fake_gateway,
        {
            "number": 42,
            "title": "Add slugify",
            "state": "open",
            "body": "spec",
            "url": "https://github.test/i/42",
        },
    )
    runs.start(RUN_ID, issue_run_task(42), "implement", company="hello-saas")
    issue = asyncio.run(activities.read_issue(RUN_ID, 42))
    assert issue["title"] == "Add slugify" and issue["body"] == "spec"
    assert runs.get(RUN_ID).ticket_urls == ["https://github.test/i/42"]


def test_read_issue_refuses_a_closed_issue(activities, monkeypatch, fake_gateway):
    """Implement consumes open work — a closed ticket is finished, not a
    spec to re-run."""
    _issue_gateway(
        monkeypatch,
        fake_gateway,
        {"number": 42, "title": "t", "state": "closed", "body": "", "url": "u"},
    )
    with pytest.raises(RuntimeError, match="not open"):
        asyncio.run(activities.read_issue(RUN_ID, 42))


def test_sandbox_policy_comes_from_the_applied_blueprint(activities):
    """The infra IR in the
    registry is the SandboxPolicy source; the logical default image resolves
    to the deploy-time reference the cluster actually carries."""
    policy = activities._sandbox_policy(RUN_ID)
    assert policy.image == "poseidon-sandbox:abc123"
    assert policy.deadline_s == 3600
    # The declared test command, and nothing else: no proxy is configured.
    assert policy.env["TEST_CMD"] == "pytest -q"
    # The per-run egress credential rides the same env, minted from the
    # worker's proxy settings.
    assert policy.env["HTTPS_PROXY"].startswith("http://run-test-0001:")


def test_custom_toolchain_image_passes_through(activities, companies):
    spec = dict(APPLIED_SPEC)
    spec["infra"] = {
        "sandbox": {**APPLIED_SPEC["infra"]["sandbox"], "image": "python:3.12-slim"}
    }
    companies.upsert("hello-saas", spec)
    assert activities._sandbox_policy(RUN_ID).image == "python:3.12-slim"


def test_provision_without_an_applied_blueprint_fails_closed(settings, backend, runs):
    lone = ImplementActivities(settings, backend, runs, InMemoryCompanies())
    with pytest.raises(RuntimeError, match="no applied blueprint"):
        lone._sandbox_policy(RUN_ID)
    assert backend.provisioned == []


def test_sandbox_policy_injects_per_run_proxy_credentials(activities, settings):
    # localhost, not a service name: the sandbox has no DNS, so the proxy
    # host is resolved here and the env carries an address.
    settings.egress_proxy_url = "http://localhost:3128"
    settings.egress_proxy_key = "test-key"
    env = activities._sandbox_policy(RUN_ID).env
    assert env["HTTPS_PROXY"].startswith(f"http://{RUN_ID}:")
    assert env["HTTPS_PROXY"].endswith("@127.0.0.1:3128")
    # Distinct runs get distinct credentials — that is the attribution.
    other = activities._sandbox_policy("run-other").env
    assert other["HTTPS_PROXY"] != env["HTTPS_PROXY"]


def test_sandbox_policy_fails_when_the_proxy_host_does_not_resolve(activities, settings):
    """Provisioning with an unresolvable proxy would hand the sandbox a
    hostname it has no resolver for — fail here, where there is one."""
    from sandbox.egressproxy.credentials import ProxyUnresolvable

    settings.egress_proxy_url = "http://nothing.invalid:3128"
    settings.egress_proxy_key = "test-key"
    with pytest.raises(ProxyUnresolvable):
        activities._sandbox_policy(RUN_ID)


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


EPHEMERAL_POD_URL = "http://10.0.0.7:8000"


def test_dispatch_agent_polls_to_terminal(activities, monkeypatch):
    transport = FakeAgentTransport(
        dispatch_codes=[202],
        statuses=[
            {"status": "running"},
            {"status": "done", "pr_url": "https://pr", "iterations": 2, "error": None},
        ],
    )
    _patch_agent(monkeypatch, transport)
    status = asyncio.run(
        activities.dispatch_agent(RUN_ID, EPHEMERAL_POD_URL, "implement", "task")
    )
    assert status["status"] == "done"
    assert status["pr_url"] == "https://pr"


def test_dispatch_agent_tolerates_conflict_and_reattaches(activities, monkeypatch):
    """A retry after worker death re-dispatches, gets 409, and polls the
    already-running agent to completion."""
    transport = FakeAgentTransport(
        dispatch_codes=[409],
        statuses=[{"status": "done", "pr_url": None, "iterations": 1, "error": "red"}],
    )
    _patch_agent(monkeypatch, transport)
    status = asyncio.run(
        activities.dispatch_agent(RUN_ID, EPHEMERAL_POD_URL, "implement", "task")
    )
    assert transport.dispatches == 1
    assert status["error"] == "red"


def test_dispatch_agent_raises_when_agent_lost_the_run(activities, monkeypatch):
    from orchestrator.clients import AgentRunLost

    transport = FakeAgentTransport(dispatch_codes=[409], statuses=[{"__code": 404}])
    _patch_agent(monkeypatch, transport)
    with pytest.raises(AgentRunLost):
        asyncio.run(
            activities.dispatch_agent(RUN_ID, EPHEMERAL_POD_URL, "implement", "task")
        )


def test_platform_verify_maps_exit_code(activities, monkeypatch, fake_gateway):
    """The gate of record: green is exit_code 0 from the platform's own
    sandbox.run_tests call, anything else is red with the output kept."""
    import orchestrator.activities.implement as module

    async def run_tests(self, run_id, paths=None):
        return {"exit_code": 1, "stdout": "1 failed", "stderr": ""}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(run_tests=run_tests))
    verdict = asyncio.run(activities.platform_verify(RUN_ID))
    assert verdict == {"passed": False, "output": "1 failed"}
