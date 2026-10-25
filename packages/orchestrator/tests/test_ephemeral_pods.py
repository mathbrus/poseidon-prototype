"""The provisioner surface without a cluster: the pod manifest's containment
posture, and the provision/destroy activities' binding, audit, and
label-selector discipline (via a fake provisioner)."""

import asyncio
import threading

import pytest
from temporalio.exceptions import ApplicationError

from common.audit import (
    LIFECYCLE_DESTROY_POD,
    LIFECYCLE_FULFILL,
    LIFECYCLE_PROVISION_POD,
    LIFECYCLE_RECORD_REQUEST,
)
from common.policy import CREW_REQUEST
from common.topology import (
    COMPANY_NAMESPACE,
    EPHEMERAL_KIND,
    PLATFORM_NAMESPACE,
    TOKEN_MOUNT_DIR,
)
from orchestrator.activities.crew import CrewActivities
from orchestrator.config import WorkerSettings
from orchestrator.ephemeral_pods import (
    ephemeral_pod_manifest,
    pod_name,
    service_account_name,
)
from orchestrator.identity import ephemeral_agent_id
from registry.memory import (
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
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
        agent_image="poseidon-agent-host:abc",
        env={"GATEWAY_TOKEN_FILE": f"{TOKEN_MOUNT_DIR}/token", "MODEL": "mock-brain"},
    )
    labels = manifest["metadata"]["labels"]
    # The kind label puts the pod inside the reconciler's agent envelope
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
        # Per-pod URLs, so tests can see which agent's pod a
        # dispatch actually reached.
        return EphemeralPodHandle(
            agent_id=agent_id,
            pod_name=pod_name(agent_id),
            url=f"http://{pod_name(agent_id)}:8000",
        )

    def destroy(self, run_id: str) -> list[str]:
        self.destroyed.append(run_id)
        return [pod_name(ephemeral_agent_id("feature-delivery", "implementer", run_id))]


def _crew(
    settings,
    participants,
    audit,
    provisioner=None,
    companies=None,
    runs=None,
    ledger=None,
):
    return CrewActivities(
        settings,
        participants,
        audit,
        companies or _companies(),
        ledger or InMemoryBudgetLedger(),
        runs or InMemoryRuns(),
        provisioner,
    )


def settings(tmp_path) -> WorkerSettings:
    token_file = tmp_path / "token"
    token_file.write_text("projected")
    return WorkerSettings(
        opa_url="http://opa.test:8181",
        platform_namespace=PLATFORM_NAMESPACE,
        sandbox_image="poseidon-sandbox:abc123",
        egress_proxy_url="http://127.0.0.1:3128",
        egress_proxy_key="egress-key",
        timeout_profile="default",
        temporal_address="temporal.test:7233",
        gateway_token_file=str(token_file),
        mcp_gateway_url="http://gateway.test:8080",
        agent_image="poseidon-agent-host:abc123",
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
    assert created["agent_image"] == "poseidon-agent-host:abc123"
    env = created["env"]
    # No credential in the env: the pod's identity is the projected token
    # of its own ServiceAccount, mounted by the kubelet.
    assert env["GATEWAY_TOKEN_FILE"] == f"{TOKEN_MOUNT_DIR}/token"
    assert "GATEWAY_TOKEN" not in env
    # The agent host's wiring, FQDN'd for the company namespace.
    assert env["MCP_GATEWAY_URL"].startswith(f"http://mcp-gateway.{PLATFORM_NAMESPACE}")
    assert env["LLM_PROXY_URL"].startswith(f"http://llm-gateway.{PLATFORM_NAMESPACE}")

    # The binding row exists (a provision retry revives it) and carries
    # the SA name the gateway resolves.
    row = participants.active(result["agent_id"])
    assert row is not None
    assert row["service_account"] == service_account_name(result["agent_id"])

    event = audit.events[-1]
    assert event.source == "process-lifecycle"
    assert event.tool == LIFECYCLE_PROVISION_POD
    assert event.rule == "ephemeral_pod_provisioned"


def test_provision_heartbeats_while_the_pod_comes_up(settings, stores, monkeypatch):
    """fulfill_crew provisions one pod per agent back to back, and a pod
    can take longer to come up than the activity heartbeat window —
    liveness must be signalled during the provision call itself. The pod
    here comes up only once the activity has said it is alive, so a beat
    sent after the call returns never arrives in time."""
    import orchestrator.activities.crew as module

    beat = threading.Event()
    monkeypatch.setattr(module.activity, "in_activity", lambda: True)
    monkeypatch.setattr(module.activity, "heartbeat", beat.set)

    class ShortWaits:
        """The module's asyncio, its waits between beats cut to a blink."""

        def __getattr__(self, name):
            return getattr(asyncio, name)

        @staticmethod
        async def wait(tasks, timeout=None):
            return await asyncio.wait(tasks, timeout=timeout and 0.01)

    monkeypatch.setattr(module, "asyncio", ShortWaits())

    class SlowPod(FakeProvisioner):
        def provision(self, **kwargs):
            assert beat.wait(timeout=5), "no heartbeat while the pod came up"
            return super().provision(**kwargs)

    participants, audit = stores
    slow = SlowPod()
    crew = _crew(settings, participants, audit, slow)
    asyncio.run(crew.provision_ephemeral_pod(dict(SPEC)))
    assert len(slow.provisioned) == 1


def test_destroy_audits_each_pod(crew, stores, provisioner):
    _, audit = stores
    asyncio.run(crew.destroy_ephemeral_pods(RUN_ID))
    assert provisioner.destroyed == [RUN_ID]
    assert [e.tool for e in audit.events] == [LIFECYCLE_DESTROY_POD]
    assert audit.events[0].rule == "ephemeral_pod_destroyed"


def test_provision_without_a_provisioner_fails_closed(settings, stores):
    participants, audit = stores
    lone = _crew(settings, participants, audit, provisioner=None)
    with pytest.raises(RuntimeError, match="provisioner"):
        asyncio.run(lone.provision_ephemeral_pod(dict(SPEC)))
    assert participants.rows == {}


# -- fulfill_crew (the fulfillment composition) ------------------------------


def _companies() -> "InMemoryCompanies":
    """The applied company every provision reads its repository from."""
    from registry.memory import InMemoryCompanies

    store = InMemoryCompanies()
    store.upsert("hello-saas", _company([IMPLEMENTER]))
    return store


def _company(agents: list[dict]) -> dict:
    return {
        "namespace": COMPANY_NAMESPACE,
        "repo": {"url": "github.com/example/repo", "base_ref": "main"},
        "vision": "ship it",
        "ephemeralAgents": agents,
    }


IMPLEMENTER = {"crew": "feature-delivery", "role": "implementer"}


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


@pytest.fixture
def fulfill_world(settings, provisioner, monkeypatch):
    """A company with one implementer crew and a compiled decision that
    allows the PM to have it fulfilled; `opa` captures what was asked."""
    import common.opa as opa_module
    from registry.memory import InMemoryAuditStore, InMemoryRunParticipants

    participants = InMemoryRunParticipants()
    audit = InMemoryAuditStore()
    companies = _companies()
    companies.upsert("hello-saas", _company([IMPLEMENTER]))
    opa = FakeOpa({"allow": True, "rule": "role_pm_crew_request"})
    monkeypatch.setattr(opa_module, "httpx", opa)
    crew = _crew(settings, participants, audit, provisioner, companies)
    return crew, participants, audit, opa, companies


def _fulfill_spec(**overrides):
    return {
        "run_id": RUN_ID,
        "company": "hello-saas",
        "crew_template": "feature-delivery",
        "conductor": {"agent_id": "pm-1", "role": "pm", "url": "http://pm:8000"},
        "workflow": "implement",
        **overrides,
    }


def test_fulfill_binds_and_provisions_the_crew(fulfill_world, provisioner):
    crew, participants, audit, _, _ = fulfill_world

    result = asyncio.run(crew.fulfill_crew(_fulfill_spec()))

    # The agent is bound with parentage and got its own pod; the result
    # names it by role with its identity and dispatch address.
    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", RUN_ID)
    assert participants.active(implementer_id)["parent_agent_id"] == "pm-1"
    assert {p["agent_id"] for p in provisioner.provisioned} == {implementer_id}
    assert result == {
        "agents": {
            "implementer": {
                "agent_id": implementer_id,
                "url": f"http://{pod_name(implementer_id)}:8000",
            }
        }
    }

    # A retry converges on the same identity instead of duplicating.
    again = asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert again["agents"]["implementer"]["agent_id"] == implementer_id

    tools = [e.tool for e in audit.events]
    assert LIFECYCLE_FULFILL in tools


def test_fulfill_refuses_a_crew_with_more_than_one_agent(fulfill_world):
    """Provisioning handles any number of agents, but no workflow defines
    a multi-agent dispatch, so a crew declaring two is refused before any
    pod exists."""
    crew, participants, _, _, companies = fulfill_world
    companies.upsert(
        "hello-saas",
        _company([IMPLEMENTER, {**IMPLEMENTER, "role": "second"}]),
    )
    with pytest.raises(RuntimeError, match="declares 2 agents"):
        asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert participants.rows == {}


# -- the crew.request decision, evaluated as the conductor -------------------


def test_fulfilment_wins_the_crew_request_decision_as_the_conductor(fulfill_world):
    """Before anything is bound, the compiled crew.request decision is
    evaluated at OPA under the conductor's enriched input, and audited."""
    crew, participants, audit, opa, _ = fulfill_world

    result = asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert set(result["agents"]) == {"implementer"}

    # The decision went to the company's compiled package with the
    # enriched conductor input — not to some hand-rolled check.
    ((url, payload),) = opa.calls
    assert url.endswith("/v1/data/poseidon/authz/hello_saas/decision")
    decision_input = payload["input"]
    assert decision_input["agent_id"] == "pm-1"
    assert decision_input["lifecycle"] == "standing"
    assert decision_input["tool"] == CREW_REQUEST
    assert decision_input["args"] == {"crew_template": "feature-delivery"}
    assert decision_input["active_spawns"] == 0
    assert decision_input["budget_headroom"] is True

    event = next(e for e in audit.events if e.tool == LIFECYCLE_RECORD_REQUEST)
    assert event.decision == "allow" and event.rule == "role_pm_crew_request"
    assert event.lifecycle == "standing" and event.agent_id == "pm-1"


@pytest.mark.parametrize(("spent", "headroom"), [(4.99, True), (5.0, False)])
def test_the_decision_input_counts_the_conductors_crews_and_the_runs_headroom(
    settings, provisioner, monkeypatch, spent, headroom
):
    """The decision input carries what policy needs beyond the caller: the
    crews the conductor already has standing, and whether this run's own
    spend is still under the blueprint's per-run cap — at the cap it has
    no headroom. Another run's spend is not this run's."""
    import common.opa as opa_module

    opa = FakeOpa({"allow": True, "rule": "role_pm_crew_request"})
    monkeypatch.setattr(opa_module, "httpx", opa)
    companies = _companies()
    companies.upsert(
        "hello-saas", {**_company([IMPLEMENTER]), "budgets": {"per_run_usd": 5.0}}
    )
    ledger = InMemoryBudgetLedger()
    for run_id, usd in ((RUN_ID, spent), ("run-other", 50.0)):
        ledger.record(
            company="hello-saas",
            run_id=run_id,
            agent_id="pm-1",
            parent_agent_id="",
            role="pm",
            model="mock-brain",
            prompt_tokens=1,
            completion_tokens=1,
            cached_tokens=0,
            reasoning_tokens=0,
            usd=usd,
        )
    participants = InMemoryRunParticipants()
    participants.add(
        "feature-review-reviewer-run-other",
        "run-other",
        "pm-1",
        "feature-review",
        "reviewer",
    )
    crew = _crew(
        settings,
        participants,
        InMemoryAuditStore(),
        provisioner,
        companies,
        ledger=ledger,
    )
    asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    ((_, payload),) = opa.calls
    assert payload["input"]["active_spawns"] == 1
    assert payload["input"]["budget_headroom"] is headroom


def test_the_decision_names_the_calling_workflow(fulfill_world):
    """The review workflow fulfils under the same authority with its own
    name in the decision input."""
    crew, _, _, opa, _ = fulfill_world
    asyncio.run(crew.fulfill_crew(_fulfill_spec(workflow="review")))
    ((_, payload),) = opa.calls
    assert payload["input"]["workflow"] == "review"


def test_fulfilment_denied_by_the_compiled_decision(fulfill_world, monkeypatch):
    """A ticket naming a crew outside may_spawn dies here, audited, with
    nothing bound and nothing provisioned."""
    import common.opa as opa_module

    crew, participants, audit, _, _ = fulfill_world
    monkeypatch.setattr(
        opa_module, "httpx", FakeOpa({"allow": False, "rule": "default_deny"})
    )

    with pytest.raises(ApplicationError, match="default_deny") as caught:
        asyncio.run(crew.fulfill_crew(_fulfill_spec(crew_template="data-export")))
    assert participants.rows == {}  # nothing bound on a deny
    event = next(e for e in audit.events if e.tool == LIFECYCLE_RECORD_REQUEST)
    assert event.decision == "deny"
    # Policy's answer is final: no retry re-asks it.
    assert caught.value.non_retryable


def test_fulfilment_with_no_policy_installed_fails_closed(fulfill_world, monkeypatch):
    """An empty OPA answer (no compiled bundle active) must refuse, not
    improvise."""
    import common.opa as opa_module

    crew, participants, _, _, _ = fulfill_world
    monkeypatch.setattr(opa_module, "httpx", FakeOpa(None))

    with pytest.raises(ApplicationError, match="no_policy_installed") as caught:
        asyncio.run(crew.fulfill_crew(_fulfill_spec()))
    assert participants.rows == {}
    # Policy's answer is final: no retry re-asks it.
    assert caught.value.non_retryable


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
        agent_image="poseidon-agent-host:test",
        env={},
    )
    assert manifest["spec"]["activeDeadlineSeconds"] == EPHEMERAL_POD_DEADLINE_S
    assert manifest["spec"]["restartPolicy"] == "Never"


def test_ephemeral_pod_runs_non_root_with_no_escalation_path():
    manifest = ephemeral_pod_manifest(
        agent_id="feature-delivery-implementer-run-1",
        run_id="run-1",
        company="hello-saas",
        agent_image="poseidon-agent-host:test",
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


def test_a_provision_retry_that_outlived_its_run_gets_no_pod(settings, stores):
    """The guard's other half. A provision retry re-asserts its binding
    row — one teardown's sweep never saw, say, because the retry bound it
    after the sweep passed — so the row alone cannot tell a live run from
    a dead one; the run's own terminal state can, and no retry re-creates
    that."""
    from orchestrator.ephemeral_pods import EphemeralPodAbandoned

    participants, audit = stores
    runs = InMemoryRuns()
    runs.start(RUN_ID, "implement issue #7", "implement", company="hello-saas")
    provisioner = FakeProvisioner()
    crew = _crew(settings, participants, audit, provisioner, runs=runs)
    # While the run is live, a provision gets its pod.
    asyncio.run(crew.provision_ephemeral_pod(dict(SPEC)))
    assert len(provisioner.provisioned) == 1

    runs.finish(RUN_ID, "failed", error="run cancelled")
    with pytest.raises(EphemeralPodAbandoned):
        asyncio.run(crew.provision_ephemeral_pod(dict(SPEC)))
    agent_id = ephemeral_agent_id("feature-delivery", "implementer", RUN_ID)
    assert participants.active(agent_id) is not None
    assert len(provisioner.provisioned) == 1, "a pod was created for a dead run"
