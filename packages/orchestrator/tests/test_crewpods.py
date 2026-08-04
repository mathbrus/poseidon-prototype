"""The 16c surface without a cluster: the pod manifest's containment
posture, and the provision/destroy activities' minting, audit, and
label-selector discipline (via a fake provisioner)."""

import asyncio

import pytest

from common import platformjwt
from orchestrator.activities.crew import CrewActivities
from orchestrator.config import WorkerSettings
from orchestrator.crewpods import CREW_KIND, crew_pod_manifest, pod_name
from orchestrator.identity import ephemeral_agent_id
from registry.memory import InMemoryAuditStore, InMemoryRunParticipants

RUN_ID = "run-01PODS"
KEY = "unit-test-platform-key"

SPEC = {
    "run_id": RUN_ID,
    "company": "hello-saas",
    "crew_template": "feature-delivery",
    "member": "executor",
    "role_template": "backend-engineer",
    "parent_agent_id": "pm-1",
}


# -- the manifest (the review artifact's runtime shape) ----------------------


def test_manifest_is_contained_and_labeled():
    agent_id = ephemeral_agent_id("feature-delivery", "executor", RUN_ID)
    manifest = crew_pod_manifest(
        agent_id=agent_id,
        run_id=RUN_ID,
        company="hello-saas",
        agent_image="poseidon-agent:abc",
        env={"GATEWAY_TOKEN": "jwt", "MODEL": "mock-brain"},
    )
    labels = manifest["metadata"]["labels"]
    # The kind label puts the pod inside the operator's agent envelope
    # (gateways-only egress, orchestrator-only ingress) the moment it
    # exists; the run label is the destroy selector's other half.
    assert labels["poseidon.dev/kind"] == CREW_KIND
    assert labels["poseidon.io/run-id"] == RUN_ID

    spec = manifest["spec"]
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["restartPolicy"] == "Never"
    (container,) = spec["containers"]
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["GATEWAY_TOKEN"] == "jwt"
    assert env["GPG_KEY"] == ""  # the base image's baked-in value, blanked
    assert container["resources"]["limits"]["memory"] == "1Gi"


def test_pod_name_fits_kubernetes():
    agent_id = ephemeral_agent_id("feature-delivery", "executor",
                                  "run-01HXXXXXXXXXXXXXXXXXXXXXXXXX")
    assert len(pod_name(agent_id)) <= 63


# -- the activities ----------------------------------------------------------


class FakeProvisioner:
    def __init__(self) -> None:
        self.provisioned: list[dict] = []
        self.destroyed: list[str] = []

    def provision(self, *, agent_id, run_id, company, agent_image, env):
        from orchestrator.crewpods import CrewPodHandle

        self.provisioned.append({
            "agent_id": agent_id, "run_id": run_id, "company": company,
            "agent_image": agent_image, "env": dict(env),
        })
        return CrewPodHandle(agent_id=agent_id, pod_name=pod_name(agent_id),
                             url="http://10.0.0.7:8000")

    def destroy(self, run_id: str) -> list[str]:
        self.destroyed.append(run_id)
        return [pod_name(ephemeral_agent_id("feature-delivery", "executor", run_id))]


@pytest.fixture
def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        agent_url="http://agent.test:8000",
        target_repo="github.com/example/repo",
        platform_jwt_key=KEY,
        agent_image="poseidon-agent:abc123",
        model="mock-brain",
    )


@pytest.fixture
def stores():
    return InMemoryRunParticipants(), InMemoryAuditStore()


@pytest.fixture
def provisioner() -> FakeProvisioner:
    return FakeProvisioner()


@pytest.fixture
def crew(settings, stores, provisioner) -> CrewActivities:
    participants, audit = stores
    return CrewActivities(settings, participants, audit, provisioner)


def test_provision_injects_a_fresh_token_and_binds(crew, stores, provisioner):
    participants, audit = stores
    result = asyncio.run(crew.provision_crew_pod(dict(SPEC)))

    (created,) = provisioner.provisioned
    assert created["agent_image"] == "poseidon-agent:abc123"
    env = created["env"]
    # The §2b credential is injected at provision time — and verifiable.
    claims = platformjwt.verify(KEY, env["GATEWAY_TOKEN"], "poseidon-gateway")
    assert claims["sub"] == result["agent_id"]
    assert claims["run_id"] == RUN_ID
    # The brain host's wiring, FQDN'd for the company namespace.
    assert env["MCP_GATEWAY_URL"].startswith("http://mcp-gateway.poseidon-platform")
    assert env["LLM_PROXY_URL"].startswith("http://llm-gateway.poseidon-platform")

    # The binding row exists (a provision retry revives it).
    assert participants.active(result["agent_id"]) is not None

    event = audit.events[-1]
    assert event.source == "process-lifecycle"
    assert event.tool == "lifecycle.provision_pod"
    assert event.rule == "crew_pod_provisioned"
    # The token never reaches the audit store.
    assert env["GATEWAY_TOKEN"] not in event.args_digest


def test_destroy_audits_each_pod(crew, stores, provisioner):
    _, audit = stores
    deleted = asyncio.run(crew.destroy_crew_pods(RUN_ID))
    assert provisioner.destroyed == [RUN_ID]
    assert deleted and all(name.startswith("crew-") for name in deleted)
    assert [e.tool for e in audit.events] == ["lifecycle.destroy_pod"]
    assert audit.events[0].rule == "crew_pod_destroyed"


def test_provision_without_a_provisioner_fails_closed(settings, stores):
    participants, audit = stores
    lone = CrewActivities(settings, participants, audit, provisioner=None)
    with pytest.raises(RuntimeError, match="provisioner"):
        asyncio.run(lone.provision_crew_pod(dict(SPEC)))
    assert participants.rows == {}


# -- fulfill_crew (the 16d composition) --------------------------------------


class FakePlanHost:
    """Answers AgentClient's dispatch/status at the httpx layer."""

    def __init__(self, brief: str = "1. do the thing") -> None:
        self.brief = brief
        self.dispatches: list[dict] = []

    def handler(self, request):
        import httpx
        import json as _json

        if request.method == "POST" and request.url.path == "/task":
            self.dispatches.append(_json.loads(request.content))
            return httpx.Response(202, json={})
        if request.method == "GET" and request.url.path.startswith("/task/"):
            return httpx.Response(200, json={
                "status": "done", "pr_url": None, "iterations": 0,
                "error": None, "brief": self.brief,
            })
        return httpx.Response(404)


@pytest.fixture
def fulfill_world(settings, provisioner, monkeypatch):
    from registry.memory import (
        InMemoryAuditStore,
        InMemoryCompanies,
        InMemoryCrewRequests,
        InMemoryRunParticipants,
    )

    participants = InMemoryRunParticipants()
    audit = InMemoryAuditStore()
    requests = InMemoryCrewRequests()
    companies = InMemoryCompanies()
    companies.upsert("hello-saas", {
        "namespace": "hello-saas",
        "crewMembers": [
            {"crew": "feature-delivery", "member": "planner",
             "role_template": "backend-engineer", "needs_sandbox": False},
            {"crew": "feature-delivery", "member": "executor",
             "role_template": "backend-engineer", "needs_sandbox": True},
        ],
    })
    plan_host = FakePlanHost()

    import httpx

    from orchestrator import clients

    original_init = clients.AgentClient.__init__

    def patched(self, url: str) -> None:
        original_init(self, url)
        self._http = httpx.AsyncClient(
            base_url=url, transport=httpx.MockTransport(plan_host.handler)
        )

    monkeypatch.setattr(clients.AgentClient, "__init__", patched)
    crew = CrewActivities(settings, participants, audit, provisioner,
                          requests, companies)
    return crew, participants, audit, requests, plan_host


def test_fulfill_composes_the_whole_handoff(fulfill_world, provisioner):
    crew, participants, audit, requests, plan_host = fulfill_world
    requests.record("pm-1", "feature-delivery", RUN_ID, "ship ticket 7")

    result = asyncio.run(crew.fulfill_crew({
        "run_id": RUN_ID, "company": "hello-saas",
        "crew_template": "feature-delivery", "conductor": "pm-1",
        "task_text": "the raw goal",
    }))

    # Both members minted with parentage; only the executor got a pod.
    assert result["members"] == ["executor", "planner"]
    planner_id = ephemeral_agent_id("feature-delivery", "planner", RUN_ID)
    executor_id = ephemeral_agent_id("feature-delivery", "executor", RUN_ID)
    assert participants.active(planner_id)["parent_agent_id"] == "pm-1"
    assert participants.active(executor_id) is not None
    assert len(provisioner.provisioned) == 1
    assert result["executor_agent_id"] == executor_id
    assert result["executor_url"] == "http://10.0.0.7:8000"

    # The planner borrowed the executor's brain host under its OWN token
    # (§4.4), in plan mode, and its brief is the handoff.
    (dispatch,) = plan_host.dispatches
    assert dispatch["mode"] == "plan"
    claims = platformjwt.verify(KEY, dispatch["gateway_token"], "poseidon-gateway")
    assert claims["sub"] == planner_id
    assert result["brief"] == "1. do the thing"

    # The request is consumed; a retry converges instead of duplicating.
    assert requests.rows[list(requests.rows)[0]]["status"] == "fulfilled"
    again = asyncio.run(crew.fulfill_crew({
        "run_id": RUN_ID, "company": "hello-saas",
        "crew_template": "feature-delivery", "conductor": "pm-1",
        "task_text": "the raw goal",
    }))
    assert again["executor_agent_id"] == executor_id

    tools = [e.tool for e in audit.events]
    assert "lifecycle.fulfill" in tools


def test_fulfill_without_recorded_intent_refuses(fulfill_world):
    crew, participants, _, _, _ = fulfill_world
    with pytest.raises(RuntimeError, match="no recorded crew request"):
        asyncio.run(crew.fulfill_crew({
            "run_id": RUN_ID, "company": "hello-saas",
            "crew_template": "feature-delivery", "conductor": "pm-1",
            "task_text": "t",
        }))
    assert participants.rows == {}  # nothing minted without authority
