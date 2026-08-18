"""Unit tests for the implement activities: step behavior and retry safety
against fakes. Sequencing, compensation and durability are workflow
concerns, proven live by the kind e2e suite (worker-kill included)."""

import asyncio
from typing import Any

import pytest

from orchestrator.activities.implement import ImplementActivities
from orchestrator.config import WorkerSettings
from orchestrator.workflows.implement import issue_placeholder
from registry.memory import InMemoryCompanies, InMemoryRuns
from sandbox.contract import SandboxHandle, SandboxPolicy

RUN_ID = "run-test-0001"

# What an applied hello-saas blueprint records (the Company CR spec's infra
# section) — the SandboxPolicy source.
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
    # The workflow id derives from the run id and is recorded with it.
    assert record.workflow_id == f"implement-{RUN_ID}"


def test_create_run_is_retry_safe(activities, runs):
    asyncio.run(activities.create_run(RUN_ID, "attempt one"))
    asyncio.run(activities.create_run(RUN_ID, "attempt two"))
    assert runs.get(RUN_ID).task == "attempt two"


def _issue_gateway(monkeypatch, issue: dict[str, Any]):
    import orchestrator.activities.implement as module

    class FakeGateway:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def read_issue(self, run_id, repo, number):
            return issue

    monkeypatch.setattr(module, "GatewayClient", FakeGateway)


def test_read_issue_returns_the_view_and_links_the_ticket(
    activities, runs, monkeypatch
):
    _issue_gateway(
        monkeypatch,
        {
            "number": 42,
            "title": "Add slugify",
            "state": "open",
            "body": "spec",
            "url": "https://github.test/i/42",
        },
    )
    asyncio.run(activities.create_run(RUN_ID, issue_placeholder(42)))
    issue = asyncio.run(activities.read_issue(RUN_ID, 42))
    assert issue["title"] == "Add slugify" and issue["body"] == "spec"
    assert runs.get(RUN_ID).ticket_urls == ["https://github.test/i/42"]


def test_read_issue_refuses_a_closed_issue(activities, monkeypatch):
    """Implement consumes open work — a closed ticket is finished, not a
    spec to re-run."""
    _issue_gateway(
        monkeypatch,
        {"number": 42, "title": "t", "state": "closed", "body": "", "url": "u"},
    )
    with pytest.raises(RuntimeError, match="not open"):
        asyncio.run(activities.read_issue(RUN_ID, 42))


JOB_HANDLE = {
    "run_id": RUN_ID,
    "url": "http://192.168.0.9:8000",
    "namespace": "hello-saas",
    "job_name": "poseidon-sbx-run-test-0001",
    "pod_name": "poseidon-sbx-run-test-0001-abcde",
}


def test_sandbox_policy_comes_from_the_applied_blueprint(activities):
    """The infra IR in the
    registry is the SandboxPolicy source; the logical default image resolves
    to the deploy-time reference the cluster actually carries."""
    policy = activities._sandbox_policy(RUN_ID)
    assert policy.image == "poseidon-sandbox:abc123"
    assert policy.deadline_s == 3600
    assert policy.egress_allow == ["pypi.org"]
    assert policy.env == {}  # no proxy configured -> nothing injected


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
        activities.teardown(RUN_ID, dict(JOB_HANDLE), None, "dispatch_executor: boom")
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert record["status"] == "failed"
    assert record["error"] == "dispatch_executor: boom"


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


CREW_POD_URL = "http://10.0.0.7:8000"


def test_dispatch_executor_polls_to_terminal(activities, monkeypatch):
    transport = FakeAgentTransport(
        dispatch_codes=[202],
        statuses=[
            {"status": "running"},
            {"status": "done", "pr_url": "https://pr", "iterations": 2, "error": None},
        ],
    )
    _patch_agent(monkeypatch, transport)
    status = asyncio.run(activities.dispatch_executor(RUN_ID, "task", CREW_POD_URL))
    assert status["status"] == "done"
    assert status["pr_url"] == "https://pr"


def test_dispatch_executor_tolerates_conflict_and_reattaches(activities, monkeypatch):
    """A retry after worker death re-dispatches, gets 409, and polls the
    already-running agent to completion."""
    transport = FakeAgentTransport(
        dispatch_codes=[409],
        statuses=[{"status": "done", "pr_url": None, "iterations": 1, "error": "red"}],
    )
    _patch_agent(monkeypatch, transport)
    status = asyncio.run(activities.dispatch_executor(RUN_ID, "task", CREW_POD_URL))
    assert transport.dispatches == 1
    assert status["error"] == "red"


def test_dispatch_executor_raises_when_agent_lost_the_run(activities, monkeypatch):
    from orchestrator.clients import AgentRunLost

    transport = FakeAgentTransport(dispatch_codes=[409], statuses=[{"__code": 404}])
    _patch_agent(monkeypatch, transport)
    with pytest.raises(AgentRunLost):
        asyncio.run(activities.dispatch_executor(RUN_ID, "task", CREW_POD_URL))


# -- implement_config ---------------------------------------------------------

WORKFLOWS_SPEC = {
    **APPLIED_SPEC,
    "workflows": [
        {
            "name": "implement",
            "enabled": True,
            "conductor": "pm",
            "crew": "feature-delivery",
            "max_verify_retries": 2,
        },
        {"name": "intake", "enabled": True, "conductor": "pm", "crew": None},
    ],
}


class FakeInstances:
    def __init__(self, rows):
        self.rows = rows

    def for_company(self, company):
        return [r for r in self.rows if r["company"] == company]


PM_ROW = {
    "agent_id": "pm-1",
    "company": "hello-saas",
    "hire": "pm",
    "role_template": "pm",
}


def _config_activities(settings, backend, runs, spec, instances):
    companies = InMemoryCompanies()
    if spec is not None:
        companies.upsert("hello-saas", spec)
    return ImplementActivities(settings, backend, runs, companies, None, instances)


def test_implement_config_resolves_crew_and_conductor(settings, backend, runs):
    activities = _config_activities(
        settings, backend, runs, WORKFLOWS_SPEC, FakeInstances([PM_ROW])
    )
    config = asyncio.run(activities.implement_config())
    assert config == {
        "company": "hello-saas",
        "crew_template": "feature-delivery",
        "conductor": "pm-1",
        "conductor_role": "pm",
        "max_verify_retries": 2,
    }


def test_implement_config_fails_closed_without_a_blueprint(settings, backend, runs):
    activities = _config_activities(
        settings, backend, runs, None, FakeInstances([PM_ROW])
    )
    with pytest.raises(RuntimeError, match="no enabled"):
        asyncio.run(activities.implement_config())


def test_implement_config_fails_closed_without_a_conductor_instance(
    settings, backend, runs
):
    """The blueprint names a conductor hire, but no standing instance has
    converged for it — refuse before anything is provisioned."""
    activities = _config_activities(
        settings, backend, runs, WORKFLOWS_SPEC, FakeInstances([])
    )
    with pytest.raises(RuntimeError, match="no standing instance"):
        asyncio.run(activities.implement_config())


def test_platform_verify_maps_exit_code(activities, monkeypatch):
    """The gate of record: green is exit_code 0 from the platform's own
    sandbox.run_tests call, anything else is red with the output kept."""
    import orchestrator.activities.implement as module

    class FakeGateway:
        def __init__(self, *a, **k): ...
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def run_tests(self, run_id):
            return {"exit_code": 1, "stdout": "1 failed", "stderr": ""}

    monkeypatch.setattr(module, "GatewayClient", FakeGateway)
    verdict = asyncio.run(activities.platform_verify(RUN_ID))
    assert verdict == {"passed": False, "output": "1 failed"}


def test_teardown_revokes_participants_on_every_terminal_path(
    settings, backend, runs, companies
):
    """The binding rows die with the run — on success, on failure, and
    with no sandbox handle at all (bind precedes provision)."""
    from orchestrator.activities.crew import CrewActivities
    from registry.memory import InMemoryAuditStore, InMemoryRunParticipants

    participants = InMemoryRunParticipants()
    audit = InMemoryAuditStore()
    crew = CrewActivities(settings, participants, audit)
    activities = ImplementActivities(settings, backend, runs, companies, crew)

    participants.add(
        "feature-delivery-executor-" + RUN_ID,
        RUN_ID,
        "pm-1",
        "feature-delivery",
        "executor",
    )
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "early failure"))
    assert record["status"] == "failed"
    assert participants.active("feature-delivery-executor-" + RUN_ID) is None
    assert [e.tool for e in audit.events] == ["lifecycle.revoke"]

    # Success path, with a handle: sweep still runs first.
    participants.add(
        "feature-delivery-planner-" + RUN_ID,
        RUN_ID,
        "pm-1",
        "feature-delivery",
        "planner",
    )
    record = asyncio.run(
        activities.teardown(
            RUN_ID, dict(JOB_HANDLE), {"status": "done", "pr_url": "https://pr"}, None
        )
    )
    assert record["status"] == "done"
    assert participants.active("feature-delivery-planner-" + RUN_ID) is None


def test_revoke_failure_is_loud_but_not_masking(settings, backend, runs, companies):
    """A failed sweep surfaces in the run record; the sandbox is still
    destroyed and the run still finalizes."""

    class BrokenCrew:
        async def revoke_participants(self, run_id: str):
            raise RuntimeError("registry unreachable")

    activities = ImplementActivities(settings, backend, runs, companies, BrokenCrew())
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), {"status": "done"}, None)
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert record["status"] == "failed"
    assert "participant revoke failed" in record["error"]
