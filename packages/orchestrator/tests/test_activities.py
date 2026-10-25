"""Unit tests for the implement activities — the steps between a run's
birth and its teardown — against fakes: the ticket read, sandbox policy
from the applied blueprint, the agent dispatch, and the verify gate.
Sequencing, compensation and durability are workflow concerns, proven
live by the kind e2e suite (worker-kill included)."""

import asyncio
import socket
from typing import Any

import pytest

from common.tasks import issue_run_task
from common.topology import COMPANY_NAMESPACE
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
    "namespace": COMPANY_NAMESPACE,
    "repo": {"url": "github.com/example/repo", "base_ref": "main"},
    "vision": "ship it",
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
        self.provisioned: list[tuple[str, bytes, SandboxPolicy]] = []
        self.destroyed: list[str] = []
        self.destroy_error: Exception | None = None

    def provision(
        self, run_id: str, workspace_tar: bytes, policy: SandboxPolicy
    ) -> SandboxHandle:
        self.provisioned.append((run_id, workspace_tar, policy))
        return SandboxHandle(
            run_id=run_id,
            url="http://192.168.0.9:8000",
            namespace=COMPANY_NAMESPACE,
            job_name="poseidon-sbx-run-test-0001",
            pod_name="poseidon-sbx-run-test-0001-abcde",
        )

    def destroy(self, handle: SandboxHandle) -> None:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(handle.job_name)


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
    runs.start("run-before", issue_run_task(42), "implement", company="hello-saas")
    runs.finish("run-before", "done", pr_url="https://github.test/pull/5")
    runs.mark_pr_settled("hello-saas", 5, "refused", "nobody answered within 60 minutes")
    runs.start(RUN_ID, issue_run_task(42), "implement", company="hello-saas")
    issue = asyncio.run(activities.read_issue(RUN_ID, 42))
    assert issue["title"] == "Add slugify" and issue["body"] == "spec"
    assert runs.get(RUN_ID).ticket_urls == ["https://github.test/i/42"]
    # The attempt before was refused: this one reads why.
    assert issue["refusals"] == [
        {
            "pr_url": "https://github.test/pull/5",
            "reason": "nobody answered within 60 minutes",
        }
    ]


def test_a_merge_conflict_is_named_not_retried(activities, monkeypatch, fake_gateway):
    """A head that does not merge onto the moved base is a non-retryable
    error of its own type, the conflict in its message; any other failure
    of the same fetch is an ordinary, retryable one."""
    from temporalio.exceptions import ApplicationError

    import orchestrator.activities.implement as module
    from orchestrator.clients import GatewayError

    failures = iter(
        [
            GatewayError(
                "Error executing tool repo.archive: merge conflict: f00d does not"
                " merge cleanly onto ba5e (conflicts in app.py)"
            ),
            GatewayError("git clone failed: network"),
        ]
    )

    async def fetch_workspace(self, *args):
        raise next(failures)

    monkeypatch.setattr(
        module, "GatewayClient", fake_gateway(fetch_workspace=fetch_workspace)
    )
    with pytest.raises(ApplicationError) as caught:
        asyncio.run(activities.fetch_workspace(RUN_ID, "f00d", None, "ba5e"))
    assert caught.value.type == "merge conflict" and caught.value.non_retryable
    assert caught.value.message == (
        "merge conflict: f00d does not merge cleanly onto ba5e (conflicts in app.py)"
    )
    with pytest.raises(GatewayError, match="network"):
        asyncio.run(activities.fetch_workspace(RUN_ID, "f00d", None, "ba5e"))


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
    # The sandbox runs the test command the company declared.
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
    with pytest.raises(RuntimeError, match="no company is applied"):
        lone._sandbox_policy(RUN_ID)


def test_provision_sandbox_provisions_under_the_policy_then_registers_it(
    activities, backend, monkeypatch, fake_gateway
):
    """The sandbox is provisioned from the run's workspace under the
    compiled policy, then registered at the gateway — its address,
    namespace and Job — so the run's sandbox calls reach it; the handle
    is what the workflow keeps for teardown."""
    import orchestrator.activities.implement as module

    registered: list[tuple[Any, ...]] = []

    async def register_sandbox(self, run_id, url, namespace, job_name):
        registered.append((run_id, url, namespace, job_name, len(backend.provisioned)))

    monkeypatch.setattr(
        module, "GatewayClient", fake_gateway(register_sandbox=register_sandbox)
    )
    handle = asyncio.run(activities.provision_sandbox(RUN_ID, b"workspace-tar"))

    ((run_id, workspace_tar, policy),) = backend.provisioned
    assert (run_id, workspace_tar) == (RUN_ID, b"workspace-tar")
    assert policy == activities._sandbox_policy(RUN_ID)
    # Registered after the sandbox exists, under the address it answers on.
    assert registered == [
        (
            RUN_ID,
            "http://192.168.0.9:8000",
            COMPANY_NAMESPACE,
            "poseidon-sbx-run-test-0001",
            1,
        )
    ]
    assert handle == {
        "run_id": RUN_ID,
        "url": "http://192.168.0.9:8000",
        "namespace": COMPANY_NAMESPACE,
        "job_name": "poseidon-sbx-run-test-0001",
        "pod_name": "poseidon-sbx-run-test-0001-abcde",
    }


@pytest.fixture
def dns(monkeypatch) -> dict[str, str]:
    """The names the worker's resolver answers, host to address — so no
    test asks a real one."""
    from sandbox.egressproxy import credentials

    names: dict[str, str] = {}

    class Resolver:
        @staticmethod
        def gethostbyname(host: str) -> str:
            if host not in names:
                raise socket.gaierror(f"{host}: Name or service not known")
            return names[host]

    monkeypatch.setattr(credentials, "socket", Resolver)
    return names


def test_sandbox_policy_injects_per_run_proxy_credentials(activities, settings, dns):
    # A service name: the sandbox has no DNS, so the proxy host is resolved
    # here and the env carries an address.
    dns["egress-proxy.test"] = "10.96.0.12"
    settings.egress_proxy_url = "http://egress-proxy.test:3128"
    settings.egress_proxy_key = "test-key"
    env = activities._sandbox_policy(RUN_ID).env
    assert env["HTTPS_PROXY"].startswith(f"http://{RUN_ID}:")
    assert env["HTTPS_PROXY"].endswith("@10.96.0.12:3128")
    # Distinct runs get distinct credentials — that is the attribution.
    other = activities._sandbox_policy("run-other").env
    assert other["HTTPS_PROXY"] != env["HTTPS_PROXY"]


def test_sandbox_policy_fails_when_the_proxy_host_does_not_resolve(
    activities, settings, dns
):
    """Provisioning with an unresolvable proxy would hand the sandbox a
    hostname it has no resolver for — fail here, where there is one."""
    from sandbox.egressproxy.credentials import ProxyUnresolvable

    settings.egress_proxy_url = "http://egress-proxy.test:3128"
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
        activities.dispatch_agent(
            RUN_ID, EPHEMERAL_POD_URL, "implement", "task", "BOUNDS", {}
        )
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
        activities.dispatch_agent(
            RUN_ID, EPHEMERAL_POD_URL, "implement", "task", "BOUNDS", {}
        )
    )
    assert transport.dispatches == 1
    assert status["error"] == "red"


def test_dispatch_agent_raises_when_agent_lost_the_run(activities, monkeypatch):
    from orchestrator.clients import AgentRunLost

    transport = FakeAgentTransport(dispatch_codes=[409], statuses=[{"__code": 404}])
    _patch_agent(monkeypatch, transport)
    with pytest.raises(AgentRunLost):
        asyncio.run(
            activities.dispatch_agent(
                RUN_ID, EPHEMERAL_POD_URL, "implement", "task", "BOUNDS", {}
            )
        )


def test_platform_verify_maps_exit_code(activities, monkeypatch, fake_gateway):
    """The gate of record: green is exit_code 0 from the platform's own
    sandbox.run_tests call, a failure is red with the output kept."""
    import orchestrator.activities.implement as module

    async def run_tests(self, run_id, paths=None):
        return {"exit_code": 1, "stdout": "1 failed", "stderr": ""}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(run_tests=run_tests))
    verdict = asyncio.run(activities.platform_verify(RUN_ID))
    assert verdict == {"passed": False, "output": "1 failed"}


def test_platform_verify_reads_an_empty_suite_by_scope(
    activities, monkeypatch, fake_gateway
):
    """A tree with no tests is green for the unscoped run — a fresh
    repository's first deliveries are documents — and red for the contract
    run, whose named paths must hold the tests the gate relies on."""
    import orchestrator.activities.implement as module

    async def run_tests(self, run_id, paths=None):
        return {"exit_code": 5, "stdout": "no tests ran in 0.00s", "stderr": ""}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(run_tests=run_tests))
    assert asyncio.run(activities.platform_verify(RUN_ID))["passed"] is True
    contract = asyncio.run(activities.platform_verify(RUN_ID, ["tests/contract/"]))
    assert contract == {"passed": False, "output": "no tests ran in 0.00s"}
