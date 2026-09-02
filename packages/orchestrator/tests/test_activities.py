"""Unit tests for the implement activities: step behavior and retry safety
against fakes. Sequencing, compensation and durability are workflow
concerns, proven live by the kind e2e suite (worker-kill included)."""

import asyncio
from typing import Any

import pytest

from orchestrator.activities.implement import ImplementActivities
from orchestrator.config import WorkerSettings
from orchestrator.workflows.implement import issue_placeholder
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryCompanies,
    InMemoryRuns,
)
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
            "test_command": "pytest -q",
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
    return ImplementActivities(
        settings, backend, runs, companies, InMemoryAgentInstances(), InMemoryAuditStore()
    )


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
    asyncio.run(activities.create_run(RUN_ID, issue_placeholder(42)))
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
    # The declared test command, and nothing else: no proxy is configured.
    assert policy.env == {"TEST_CMD": "pytest -q"}


def test_custom_toolchain_image_passes_through(activities, companies):
    spec = dict(APPLIED_SPEC)
    spec["infra"] = {
        "sandbox": {**APPLIED_SPEC["infra"]["sandbox"], "image": "python:3.12-slim"}
    }
    companies.upsert("hello-saas", spec)
    assert activities._sandbox_policy(RUN_ID).image == "python:3.12-slim"


def test_provision_without_an_applied_blueprint_fails_closed(settings, backend, runs):
    lone = ImplementActivities(
        settings,
        backend,
        runs,
        InMemoryCompanies(),
        InMemoryAgentInstances(),
        InMemoryAuditStore(),
    )
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
        activities.teardown(RUN_ID, dict(JOB_HANDLE), None, "dispatch_agent: boom")
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert record["status"] == "failed"
    assert record["error"] == "dispatch_agent: boom"


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
    "standing_role": "pm",
    "role_template": "pm",
    "namespace": "hello-saas",
}


def _config_activities(settings, backend, runs, spec, instances):
    companies = InMemoryCompanies()
    if spec is not None:
        companies.upsert("hello-saas", spec)
    return ImplementActivities(
        settings, backend, runs, companies, instances, InMemoryAuditStore()
    )


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
        "conductor_url": "http://pm.hello-saas.svc.cluster.local:8000",
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
    """The blueprint names a conductor standing_role, but no standing instance has
    converged for it — refuse before anything is provisioned."""
    activities = _config_activities(
        settings, backend, runs, WORKFLOWS_SPEC, FakeInstances([])
    )
    with pytest.raises(RuntimeError, match="no standing instance"):
        asyncio.run(activities.implement_config())


def test_platform_verify_maps_exit_code(activities, monkeypatch, fake_gateway):
    """The gate of record: green is exit_code 0 from the platform's own
    sandbox.run_tests call, anything else is red with the output kept."""
    import orchestrator.activities.implement as module

    async def run_tests(self, run_id, paths=None):
        return {"exit_code": 1, "stdout": "1 failed", "stderr": ""}

    monkeypatch.setattr(module, "GatewayClient", fake_gateway(run_tests=run_tests))
    verdict = asyncio.run(activities.platform_verify(RUN_ID))
    assert verdict == {"passed": False, "output": "1 failed"}


def test_teardown_revokes_participants_on_every_terminal_path(
    settings, backend, runs, companies
):
    """The binding rows die with the run — on success, on failure, and
    with no sandbox handle at all (bind precedes provision)."""
    from orchestrator.activities.crew import CrewActivities
    from registry.memory import (
        InMemoryAuditStore,
        InMemoryBudgetLedger,
        InMemoryCrewRequests,
        InMemoryRunParticipants,
    )

    participants = InMemoryRunParticipants()
    audit = InMemoryAuditStore()
    crew = CrewActivities(
        settings,
        participants,
        audit,
        InMemoryCrewRequests(),
        companies,
        InMemoryBudgetLedger(),
        runs,
    )
    activities = ImplementActivities(
        settings, backend, runs, companies, InMemoryAgentInstances(), audit, crew
    )

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
        "feature-review-reviewer-" + RUN_ID,
        RUN_ID,
        "pm-1",
        "feature-review",
        "reviewer",
    )
    record = asyncio.run(
        activities.teardown(
            RUN_ID, dict(JOB_HANDLE), {"status": "done", "pr_url": "https://pr"}, None
        )
    )
    assert record["status"] == "done"
    assert participants.active("feature-review-reviewer-" + RUN_ID) is None


def test_revoke_failure_is_loud_but_not_masking(settings, backend, runs, companies):
    """A failed sweep surfaces in the run record; the sandbox is still
    destroyed and the run still finalizes."""

    class BrokenCrew:
        async def revoke_participants(self, run_id: str):
            raise RuntimeError("registry unreachable")

    activities = ImplementActivities(
        settings,
        backend,
        runs,
        companies,
        InMemoryAgentInstances(),
        InMemoryAuditStore(),
        BrokenCrew(),
    )
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), {"status": "done"}, None)
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert record["status"] == "failed"
    assert "participant revoke failed" in record["error"]


def test_create_run_refuses_a_second_live_run_for_the_same_issue(activities, runs):
    """One live implement run per issue — the registry constraint surfaces
    here, so neither a heartbeat tick nor a human `poseidon run <n>` can
    duplicate work already in flight."""
    asyncio.run(activities.create_run(RUN_ID, issue_placeholder(42)))
    with pytest.raises(RuntimeError, match="one live run per work item"):
        asyncio.run(activities.create_run("run-elsewhere", issue_placeholder(42)))
    # The losing run never clobbered the live row.
    assert runs.get(RUN_ID).status == "running"
    assert runs.get("run-elsewhere") is None


# -- record_attempts_exhausted ---------------------------------------------------


HEARTBEAT_APPLIED_SPEC = {**APPLIED_SPEC, "heartbeat": {"max_attempts": 2}}


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


def _exhaustion_gateway(monkeypatch, fake_gateway) -> list[tuple[str, str, int, str]]:
    """The world's side of the notice: every comment the activity posts,
    as (item kind, run_id, number, body)."""
    import orchestrator.activities.implement as module

    comments: list[tuple[str, str, int, str]] = []

    async def comment_pr(self, run_id, repo, number, body):
        comments.append(("pr", run_id, number, body))
        return {}

    async def comment_issue(self, run_id, repo, number, body):
        comments.append(("issue", run_id, number, body))
        return {}

    monkeypatch.setattr(
        module,
        "GatewayClient",
        fake_gateway(comment_pr=comment_pr, comment_issue=comment_issue),
    )
    return comments


def _attempt_activities(settings, backend, runs, audit, spec=HEARTBEAT_APPLIED_SPEC):
    companies = InMemoryCompanies()
    companies.upsert("hello-saas", spec)
    return ImplementActivities(
        settings, backend, runs, companies, InMemoryAgentInstances(), audit
    )


def _aborted(runs: InMemoryRuns, run_id: str, task: str, workflow: str) -> None:
    runs.start(run_id, task, workflow, company="hello-saas")
    runs.finish(run_id, "failed", error="litellm.Timeout")


def test_the_last_aborted_review_records_exhaustion_and_tells_the_pr(
    settings, backend, runs, audit, monkeypatch, fake_gateway
):
    """Two reviews of one PR aborted without a verdict under
    `max_attempts: 2`: the second is the last the blueprint allows, so it
    writes the process-gate event under its own run id and the notice on
    the PR — the shape of `gate_rework_exhausted`, the abandonment made
    visible in the ledger and in the world."""
    comments = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-r1", "review pull request #7", "review")
    _aborted(runs, "run-r2", "review pull request #7", "review")
    activities = _attempt_activities(settings, backend, runs, audit)

    out = asyncio.run(activities.record_attempts_exhausted("run-r2", "review-run-r2"))

    assert out == {"exhausted": True, "attempts": 2}
    (event,) = audit.story("run-r2")
    assert event["tool"] == "gate.heartbeat_attempts"
    assert event["rule"] == "gate_exhausted"
    assert event["decision"] == "deny"
    assert event["source"] == "process-gate"
    assert event["workflow"] == "review-run-r2"
    assert audit.story("run-r1") == []
    ((kind, run_id, number, body),) = comments
    assert (kind, run_id, number) == ("pr", "run-r2", 7)
    assert "max_attempts" in body and "2" in body


def test_the_last_aborted_issue_run_tells_the_issue(
    settings, backend, runs, audit, monkeypatch, fake_gateway
):
    comments = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-i1", "implement issue #3", "implement")
    _aborted(runs, "run-i2", "implement issue #3", "implement")
    activities = _attempt_activities(settings, backend, runs, audit)

    out = asyncio.run(activities.record_attempts_exhausted("run-i2", "implement-run-i2"))

    assert out["exhausted"] is True
    assert [e["rule"] for e in audit.story("run-i2")] == ["gate_exhausted"]
    assert [(c[0], c[2]) for c in comments] == [("issue", 3)]


def test_an_abort_with_attempts_left_records_nothing(
    settings, backend, runs, audit, monkeypatch, fake_gateway
):
    """The first abort is not an abandonment: the item goes back on the
    backlog, and nothing is written until the cap is actually spent."""
    comments = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-i1", "implement issue #3", "implement")
    activities = _attempt_activities(settings, backend, runs, audit)

    out = asyncio.run(activities.record_attempts_exhausted("run-i1", "implement-run-i1"))

    assert out == {"exhausted": False}
    assert audit.story("run-i1") == [] and comments == []


def test_a_concluded_item_is_consumed_by_its_conclusion_not_by_exhaustion(
    settings, backend, runs, audit, monkeypatch, fake_gateway
):
    """A review that reached a verdict concluded the PR whatever the gate
    did next; a later abort on the same PR (a human's re-run) spends no
    heartbeat attempt and announces no exhaustion."""
    comments = _exhaustion_gateway(monkeypatch, fake_gateway)
    runs.start("run-r1", "review pull request #7", "review", company="hello-saas")
    runs.record_verdict("run-r1", "fail")
    runs.finish("run-r1", "failed", error="merge gate rejected")
    _aborted(runs, "run-r2", "review pull request #7", "review")
    activities = _attempt_activities(settings, backend, runs, audit)

    out = asyncio.run(activities.record_attempts_exhausted("run-r2", "review-run-r2"))

    assert out == {"exhausted": False}
    assert audit.story("run-r2") == [] and comments == []


def test_no_heartbeat_section_means_no_attempt_bound(
    settings, backend, runs, audit, monkeypatch, fake_gateway
):
    """A human-started company has no `max_attempts`: nothing offers work
    again, so nothing is exhausted — the human decides what to re-run."""
    comments = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-r1", "review pull request #7", "review")
    _aborted(runs, "run-r2", "review pull request #7", "review")
    activities = _attempt_activities(settings, backend, runs, audit, APPLIED_SPEC)

    out = asyncio.run(activities.record_attempts_exhausted("run-r2", "review-run-r2"))

    assert out == {"exhausted": False}
    assert audit.story("run-r2") == [] and comments == []


def test_a_task_file_run_is_not_heartbeat_work(
    settings, backend, runs, audit, monkeypatch, fake_gateway
):
    comments = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-t1", "the whole task file text", "implement")
    _aborted(runs, "run-t2", "the whole task file text", "implement")
    activities = _attempt_activities(settings, backend, runs, audit)

    out = asyncio.run(activities.record_attempts_exhausted("run-t2", "implement-run-t2"))

    assert out == {"exhausted": False}
    assert audit.story("run-t2") == [] and comments == []
