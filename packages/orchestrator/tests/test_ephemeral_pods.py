"""The provisioner surface without a cluster: the pod manifest's containment
posture, and the provision/destroy activities' binding, audit, and
label-selector discipline (via a fake provisioner)."""

import asyncio

import pytest

from orchestrator.activities.crew import CrewActivities
from orchestrator.config import WorkerSettings
from orchestrator.ephemeral_pods import (
    EPHEMERAL_KIND,
    TOKEN_MOUNT_DIR,
    ephemeral_pod_manifest,
    pod_name,
    service_account_name,
)
from orchestrator.identity import ephemeral_agent_id
from registry.memory import (
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryCrewRequests,
    InMemoryRunParticipants,
    InMemoryRuns,
)

RUN_ID = "run-01PODS"

SPEC = {
    "run_id": RUN_ID,
    "company": "hello-saas",
    "crew_template": "feature-delivery",
    "role": "implementer",
    "parent_agent_id": "pm-1",
}


# -- the manifest (the review artifact's runtime shape) ----------------------


def test_manifest_is_contained_and_labeled():
    agent_id = ephemeral_agent_id("feature-delivery", "implementer", RUN_ID)
    manifest = ephemeral_pod_manifest(
        agent_id=agent_id,
        run_id=RUN_ID,
        company="hello-saas",
        agent_image="poseidon-agent:abc",
        env={"GATEWAY_TOKEN_FILE": f"{TOKEN_MOUNT_DIR}/token", "MODEL": "mock-brain"},
    )
    labels = manifest["metadata"]["labels"]
    # The kind label puts the pod inside the operator's agent envelope
    # (gateways-only egress, orchestrator-only ingress) the moment it
    # exists; the run label is the destroy selector's other half.
    assert labels["poseidon.dev/kind"] == EPHEMERAL_KIND
    assert labels["poseidon.dev/run-id"] == RUN_ID

    spec = manifest["spec"]
    # The standing-agent identity posture, exactly: the pod's own SA, an
    # audience-scoped projected token as its only credential, no
    # API-server token, and nothing token-shaped in the env.
    assert spec["serviceAccountName"] == service_account_name(agent_id)
    assert spec["automountServiceAccountToken"] is False
    assert spec["enableServiceLinks"] is False
    assert spec["restartPolicy"] == "Never"
    (volume,) = spec["volumes"]
    (source,) = volume["projected"]["sources"]
    assert source["serviceAccountToken"]["audience"] == "poseidon-mcp-gateway"
    (container,) = spec["containers"]
    (mount,) = container["volumeMounts"]
    assert mount["mountPath"] == TOKEN_MOUNT_DIR and mount["readOnly"] is True
    env = {e["name"]: e["value"] for e in container["env"]}
    assert env["GATEWAY_TOKEN_FILE"] == f"{TOKEN_MOUNT_DIR}/token"
    assert "GATEWAY_TOKEN" not in env
    assert env["GPG_KEY"] == ""  # the base image's baked-in value, blanked
    assert container["resources"]["limits"]["memory"] == "1Gi"


def test_pod_name_fits_kubernetes():
    agent_id = ephemeral_agent_id(
        "feature-delivery", "implementer", "run-01HXXXXXXXXXXXXXXXXXXXXXXXXX"
    )
    assert len(pod_name(agent_id)) <= 63


# -- the activities ----------------------------------------------------------


class FakeProvisioner:
    def __init__(self) -> None:
        self.provisioned: list[dict] = []
        self.destroyed: list[str] = []

    def provision(
        self, *, agent_id, run_id, company, agent_image, env, still_wanted=None
    ):
        from orchestrator.ephemeral_pods import EphemeralPodAbandoned, EphemeralPodHandle

        # Mirrors the real provisioner's race-guard contract: the caller's
        # verdict is consulted immediately before creation, never earlier.
        # A double that skipped it could not catch a guard that stopped
        # being wired up.
        if still_wanted is not None and not still_wanted():
            raise EphemeralPodAbandoned(f"run {run_id} was torn down")
        self.provisioned.append(
            {
                "agent_id": agent_id,
                "run_id": run_id,
                "company": company,
                "agent_image": agent_image,
                "env": dict(env),
            }
        )
        # Per-pod URLs, so tests can see which agent's brain host a
        # dispatch actually reached.
        return EphemeralPodHandle(
            agent_id=agent_id,
            pod_name=pod_name(agent_id),
            url=f"http://{pod_name(agent_id)}:8000",
        )

    def destroy(self, run_id: str) -> list[str]:
        self.destroyed.append(run_id)
        return [pod_name(ephemeral_agent_id("feature-delivery", "implementer", run_id))]


def _crew(settings, participants, audit, provisioner=None, requests=None, companies=None):
    return CrewActivities(
        settings,
        participants,
        audit,
        requests or InMemoryCrewRequests(),
        companies or InMemoryCompanies(),
        InMemoryBudgetLedger(),
        InMemoryRuns(),
        provisioner,
    )


def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected")
    return WorkerSettings(
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="github.com/example/repo",
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
    return _crew(settings, participants, audit, provisioner)


def test_provision_wires_the_projected_identity_and_binds(crew, stores, provisioner):
    participants, audit = stores
    result = asyncio.run(crew.provision_ephemeral_pod(dict(SPEC)))

    (created,) = provisioner.provisioned
    assert created["agent_image"] == "poseidon-agent:abc123"
    env = created["env"]
    # No credential in the env: the pod's identity is the projected token
    # of its own ServiceAccount, mounted by the kubelet.
    assert env["GATEWAY_TOKEN_FILE"] == f"{TOKEN_MOUNT_DIR}/token"
    assert "GATEWAY_TOKEN" not in env
    # The brain host's wiring, FQDN'd for the company namespace.
    assert env["MCP_GATEWAY_URL"].startswith("http://mcp-gateway.poseidon-platform")
    assert env["LLM_PROXY_URL"].startswith("http://llm-gateway.poseidon-platform")

    # The binding row exists (a provision retry revives it) and carries
    # the SA name the gateway resolves.
    row = participants.active(result["agent_id"])
    assert row is not None
    assert row["service_account"] == service_account_name(result["agent_id"])

    event = audit.events[-1]
    assert event.source == "process-lifecycle"
    assert event.tool == "lifecycle.provision_pod"
    assert event.rule == "ephemeral_pod_provisioned"


def test_provision_heartbeats_while_the_pod_comes_up(
    crew, stores, provisioner, monkeypatch
):
    """fulfill_crew provisions one pod per agent back to back, and a pod
    can take longer to come up than the activity heartbeat window —
    liveness must be signalled during the provision call itself."""
    import orchestrator.activities.crew as module

    beats: list[int] = []
    monkeypatch.setattr(module.activity, "in_activity", lambda: True)
    monkeypatch.setattr(module.activity, "heartbeat", lambda: beats.append(1))

    asyncio.run(crew.provision_ephemeral_pod(dict(SPEC)))
    assert beats, "no heartbeat during pod provisioning"


def test_destroy_audits_each_pod(crew, stores, provisioner):
    _, audit = stores
    deleted = asyncio.run(crew.destroy_ephemeral_pods(RUN_ID))
    assert provisioner.destroyed == [RUN_ID]
    assert deleted and all(name.startswith("ephemeral-") for name in deleted)
    assert [e.tool for e in audit.events] == ["lifecycle.destroy_pod"]
    assert audit.events[0].rule == "ephemeral_pod_destroyed"


def test_provision_without_a_provisioner_fails_closed(settings, stores):
    participants, audit = stores
    lone = _crew(settings, participants, audit, provisioner=None)
    with pytest.raises(RuntimeError, match="provisioner"):
        asyncio.run(lone.provision_ephemeral_pod(dict(SPEC)))
    assert participants.rows == {}


# -- fulfill_crew (the fulfillment composition) ------------------------------


def _company(agents: list[dict]) -> dict:
    return {"namespace": "hello-saas", "ephemeralAgents": agents}


IMPLEMENTER = {"crew": "feature-delivery", "role": "implementer"}


@pytest.fixture
def fulfill_world(settings, provisioner):
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
    companies.upsert("hello-saas", _company([IMPLEMENTER]))
    crew = _crew(settings, participants, audit, provisioner, requests, companies)
    return crew, participants, audit, requests, companies


def test_fulfill_binds_and_provisions_the_crew(fulfill_world, provisioner):
    crew, participants, audit, requests, _ = fulfill_world
    requests.record("pm-1", "feature-delivery", RUN_ID, "ship ticket 7")

    result = asyncio.run(
        crew.fulfill_crew(
            {
                "run_id": RUN_ID,
                "company": "hello-saas",
                "crew_template": "feature-delivery",
                "conductor": "pm-1",
            }
        )
    )

    # The agent is bound with parentage and got its own pod; the result
    # names it by role with its identity and dispatch address.
    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", RUN_ID)
    assert participants.active(implementer_id)["parent_agent_id"] == "pm-1"
    assert {p["agent_id"] for p in provisioner.provisioned} == {implementer_id}
    assert result["agents"] == {
        "implementer": {
            "agent_id": implementer_id,
            "url": f"http://{pod_name(implementer_id)}:8000",
        }
    }

    # The request is consumed; a retry converges instead of duplicating.
    assert requests.rows[list(requests.rows)[0]]["status"] == "fulfilled"
    again = asyncio.run(
        crew.fulfill_crew(
            {
                "run_id": RUN_ID,
                "company": "hello-saas",
                "crew_template": "feature-delivery",
                "conductor": "pm-1",
            }
        )
    )
    assert again["agents"]["implementer"]["agent_id"] == implementer_id

    tools = [e.tool for e in audit.events]
    assert "lifecycle.fulfill" in tools


def test_fulfill_refuses_a_crew_with_more_than_one_agent(fulfill_world):
    """Provisioning handles any number of agents, but no workflow defines
    a multi-agent dispatch, so a crew declaring two is refused before any
    pod exists."""
    crew, participants, _, requests, companies = fulfill_world
    companies.upsert(
        "hello-saas",
        _company([IMPLEMENTER, {**IMPLEMENTER, "role": "second"}]),
    )
    requests.record("pm-1", "feature-delivery", RUN_ID, "ship ticket 7")
    with pytest.raises(RuntimeError, match="declares 2 agents"):
        asyncio.run(
            crew.fulfill_crew(
                {
                    "run_id": RUN_ID,
                    "company": "hello-saas",
                    "crew_template": "feature-delivery",
                    "conductor": "pm-1",
                }
            )
        )
    assert participants.rows == {}


def test_fulfill_without_recorded_intent_refuses(fulfill_world):
    crew, participants, _, _, _ = fulfill_world
    with pytest.raises(RuntimeError, match="no recorded crew request"):
        asyncio.run(
            crew.fulfill_crew(
                {
                    "run_id": RUN_ID,
                    "company": "hello-saas",
                    "crew_template": "feature-delivery",
                    "conductor": "pm-1",
                }
            )
        )
    assert participants.rows == {}  # nothing bound without authority


# -- record-under-authority ---------------------------------------------------


class FakeOpa:
    """Stands in for the crew module's httpx name: captures the decision
    input and answers with a canned compiled decision."""

    def __init__(self, result: dict | None) -> None:
        self.result = result
        self.calls: list[tuple[str, dict]] = []

    def AsyncClient(self, **kwargs):  # noqa: N802 — mimics the httpx surface
        fake = self

        class _Client:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return False

            async def post(self, url, json):
                import httpx as real

                fake.calls.append((url, json))
                body = {} if fake.result is None else {"result": fake.result}
                return real.Response(200, json=body, request=real.Request("POST", url))

        return _Client()


def _fulfill_spec():
    return {
        "run_id": RUN_ID,
        "company": "hello-saas",
        "crew_template": "feature-delivery",
        "conductor": "pm-1",
        "conductor_role": "pm",
        "record_under_authority": True,
    }


def test_workflow_path_records_under_conductor_authority(fulfill_world, monkeypatch):
    """No recorded request + record_under_authority: the same compiled
    crew.request decision is evaluated at OPA under the conductor's
    input, the recording is audited, and fulfillment proceeds."""
    import orchestrator.opa as opa_module

    crew, participants, audit, requests, _ = fulfill_world
    opa = FakeOpa({"allow": True, "rule": "role_pm_crew_request"})
    monkeypatch.setattr(opa_module, "httpx", opa)

    result = asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert set(result["agents"]) == {"implementer"}

    # The decision went to the company's compiled package with the
    # enriched conductor input — not to some hand-rolled check.
    ((url, payload),) = opa.calls
    assert url.endswith("/v1/data/poseidon/authz/hello_saas/decision")
    decision_input = payload["input"]
    assert decision_input["agent_id"] == "pm-1"
    assert decision_input["lifecycle"] == "standing"
    assert decision_input["tool"] == "crew.request"
    assert decision_input["args"] == {"crew_template": "feature-delivery"}
    assert decision_input["active_spawns"] == 0
    assert decision_input["budget_headroom"] is True

    # The same row the tool path writes, now fulfilled.
    row = requests.rows[list(requests.rows)[0]]
    assert row["conductor"] == "pm-1" and row["status"] == "fulfilled"

    event = next(e for e in audit.events if e.tool == "lifecycle.record_request")
    assert event.decision == "allow" and event.rule == "role_pm_crew_request"
    assert event.lifecycle == "standing" and event.agent_id == "pm-1"


def test_recording_names_the_calling_workflow(fulfill_world, monkeypatch):
    """The review workflow records under the same authority with its own
    name in the decision input — implement stays the default."""
    import orchestrator.opa as opa_module

    crew, _, _, _, _ = fulfill_world
    opa = FakeOpa({"allow": True, "rule": "role_pm_crew_request"})
    monkeypatch.setattr(opa_module, "httpx", opa)

    asyncio.run(crew.fulfill_crew({**_fulfill_spec(), "workflow": "review"}))
    ((_, payload),) = opa.calls
    assert payload["input"]["workflow"] == "review"


def test_workflow_path_denied_by_the_compiled_decision(fulfill_world, monkeypatch):
    import orchestrator.opa as opa_module

    crew, participants, audit, requests, _ = fulfill_world
    opa = FakeOpa({"allow": False, "rule": "crew_request_not_in_may_spawn"})
    monkeypatch.setattr(opa_module, "httpx", opa)

    with pytest.raises(RuntimeError, match="crew_request_not_in_may_spawn"):
        asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert participants.rows == {}  # nothing bound on a deny
    assert requests.rows == {}  # and nothing recorded
    event = next(e for e in audit.events if e.tool == "lifecycle.record_request")
    assert event.decision == "deny"


def test_workflow_path_with_no_policy_installed_fails_closed(fulfill_world, monkeypatch):
    """An empty OPA answer (no compiled bundle active) must refuse, not
    improvise."""
    import orchestrator.opa as opa_module

    crew, participants, _, _, _ = fulfill_world
    monkeypatch.setattr(opa_module, "httpx", FakeOpa(None))

    with pytest.raises(RuntimeError, match="no_policy_installed"):
        asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert participants.rows == {}


def test_ephemeral_pod_carries_a_wall_clock_deadline():
    """The race guard refuses to provision for a run
    whose teardown has passed, but a pod created in the last instant before
    that becomes true has no owner left to collect it. A deadline means an
    orphan stops burning compute by itself — the same device sandboxes
    already use."""
    from orchestrator.ephemeral_pods import EPHEMERAL_POD_DEADLINE_S

    manifest = ephemeral_pod_manifest(
        agent_id="feature-delivery-implementer-run-1",
        run_id="run-1",
        company="hello-saas",
        agent_image="poseidon-agent:test",
        env={},
    )
    assert manifest["spec"]["activeDeadlineSeconds"] == EPHEMERAL_POD_DEADLINE_S
    assert manifest["spec"]["restartPolicy"] == "Never"


def test_ephemeral_pod_runs_non_root_with_no_escalation_path():
    manifest = ephemeral_pod_manifest(
        agent_id="feature-delivery-implementer-run-1",
        run_id="run-1",
        company="hello-saas",
        agent_image="poseidon-agent:test",
        env={},
    )
    assert manifest["spec"]["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert manifest["spec"]["containers"][0]["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }


def test_a_run_torn_down_mid_provision_gets_no_pod(settings, stores):
    """The timing that matters: the guard is consulted by the provisioner
    immediately before creation, not by the activity beforehand — a
    verdict taken before the remnant-deletion wait is stale by the time
    the pod is born. Here the run is torn down *during* provisioning:
    no pod."""
    from orchestrator.ephemeral_pods import EphemeralPodAbandoned

    participants, audit = stores

    class TearsDownMidProvision(FakeProvisioner):
        def provision(self, **kwargs):
            # Stands in for the remnant-deletion wait: the run ends while
            # we are in here, before anything is created.
            participants.terminate_for_run(RUN_ID)
            return super().provision(**kwargs)

    provisioner = TearsDownMidProvision()
    crew = _crew(settings, participants, audit, provisioner)
    asyncio.run(crew.bind_participant(dict(SPEC)))

    with pytest.raises(EphemeralPodAbandoned):
        asyncio.run(crew.provision_ephemeral_pod(dict(SPEC)))
    assert provisioner.provisioned == [], "a pod was created for a dead run"
