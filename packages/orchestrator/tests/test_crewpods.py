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
