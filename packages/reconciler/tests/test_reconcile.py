"""The reconcile handlers, as pure orchestration over two seams.

Everything cluster-shaped goes through `kube.Cluster` and everything
database-shaped through the registry accessors; both are replaced with
recording fakes here, so these tests pin *what the reconciler does* — which
objects it converges, what it sweeps at teardown, what status it writes —
without a cluster or a database.
"""

import kopf
import pytest

from common.topology import PLATFORM_NAMESPACE
from reconciler import reconcile
from reconciler.config import ReconcilerSettings


class FakeCluster:
    def __init__(self) -> None:
        self.calls: list[tuple] = []

    def __getattr__(self, method: str):
        if method.startswith("upsert_") or method.startswith("delete_"):

            def record(*args):
                name = (
                    args[-1]["metadata"]["name"]
                    if isinstance(args[-1], dict)
                    else args[-1]
                )
                self.calls.append((method, *args[:-1], name))

            return record
        raise AttributeError(method)

    def named(self, method: str) -> list[tuple]:
        return [call[1:] for call in self.calls if call[0] == method]


TOMBSTONE = {}  # what a revoked company's store holds: no files


class FakeBundleStore:
    def __init__(self) -> None:
        self.stored: dict[str, dict] = {}
        self.revoked: list[str] = []

    def put(self, company: str, files: dict) -> str:
        self.stored[company] = files
        return f"rev-{company}"

    def revoke(self, company: str) -> str | None:
        if company not in self.stored:
            return None
        self.revoked.append(company)
        self.stored[company] = TOMBSTONE
        return f"tomb-{company}"


class FakeOpa:
    """OPA's activation record as the reconciler reads it. By default an
    OPA that has polled: every stored bundle — a tombstone included — is
    served at its stored revision. `lagging` names the bundles it has not
    caught up on yet."""

    def __init__(self, bundles: FakeBundleStore) -> None:
        self._bundles = bundles
        self.lagging: set[str] = set()
        self.asked: list[str] = []

    def activated_revision(self, opa_url: str, name: str) -> str | None:
        self.asked.append(name)
        if name in self.lagging or name not in self._bundles.stored:
            return None
        if self._bundles.stored[name] is TOMBSTONE:
            return f"tomb-{name}"
        return f"rev-{name}"


class FakeInstances:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def upsert(
        self,
        agent_id,
        company,
        standing_name,
        role,
        namespace=None,
        status="converged",
    ):
        self.rows[agent_id] = {
            "company": company,
            "standingName": standing_name,
            "namespace": namespace,
            "status": status,
        }

    def delete(self, agent_id: str) -> None:
        self.rows.pop(agent_id, None)

    def delete_for_company(self, company: str) -> None:
        self.rows = {k: v for k, v in self.rows.items() if v["company"] != company}


class FakeCompanies:
    def __init__(self) -> None:
        self.rows: dict[str, dict] = {}

    def upsert(self, name: str, spec: dict) -> None:
        self.rows[name] = spec

    def delete(self, name: str) -> None:
        self.rows.pop(name, None)


@pytest.fixture(autouse=True)
def operator_settings(monkeypatch) -> None:
    """The env the chart would set, without a chart."""
    monkeypatch.setattr(
        reconcile,
        "_the_settings",
        ReconcilerSettings(
            platform_namespace=PLATFORM_NAMESPACE,
            model="mock-brain",
            agent_image="poseidon-agent-host:abc123",
            opa_url="http://opa.test:8181",
        ),
    )


@pytest.fixture
def cluster(monkeypatch) -> FakeCluster:
    fake = FakeCluster()
    monkeypatch.setattr(reconcile, "_cluster", lambda: fake)
    return fake


@pytest.fixture
def stores(monkeypatch) -> tuple[FakeBundleStore, FakeInstances, FakeCompanies]:
    bundles, instances, companies = FakeBundleStore(), FakeInstances(), FakeCompanies()
    monkeypatch.setattr(reconcile, "_db", lambda: None)
    monkeypatch.setattr(reconcile, "PolicyBundles", lambda db: bundles)
    monkeypatch.setattr(reconcile, "AgentInstances", lambda db: instances)
    monkeypatch.setattr(reconcile, "Companies", lambda db: companies)
    return bundles, instances, companies


@pytest.fixture
def opa(monkeypatch, stores) -> FakeOpa:
    fake = FakeOpa(stores[0])
    monkeypatch.setattr(reconcile, "activated_revision", fake.activated_revision)
    return fake


COMPANY_SPEC = {
    "namespace": "hello-saas",
    "vision": "ship it",
    "policyBundle": {"authz.rego": "package poseidon.authz.hello_saas\n"},
    "blueprintSource": {
        "text": "company: {name: hello-saas}\n",
        "sha256": "0" * 64,
        "appliedBy": "owner",
        "appliedAt": "2026-01-01T00:00:00+00:00",
    },
    "platformBundle": {"platform.rego": "package poseidon.platform\n"},
    "infra": {
        "network_policy": {
            "namespace": "hello-saas",
            "sandbox_ingress_from": ["mcp-gateway"],
            "sandbox_egress_to": ["egress-proxy", "kube-dns"],
        }
    },
    "workflows": [],
    "budgets": {},
    "policies": {"branch_protection": {"protected": ["main"]}},
}

# What the API server stamps on the CR: the generation of the applied spec.
META = {"generation": 1}

INSTANCE_SPEC = {
    "agentId": "pm-1",
    "company": "hello-saas",
    "standingName": "pm",
    "role": "pm",
    "tools": ["issues.read", "crew.request"],
    "repo": {"url": "github.com/example/repo", "base_ref": "main"},
    "vision": "ship it",
}


def test_company_reconcile_converges_the_envelope(cluster, stores, opa):
    bundles, _instances, companies = stores
    patch = kopf.Patch()
    reconcile.reconcile_company(
        spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=patch
    )

    assert cluster.named("upsert_namespace") == [("hello-saas",)]
    assert {name for ns, name in cluster.named("upsert_network_policy")} == {
        "default-deny",
        "agent-egress",
        "agent-ingress",
        "sandbox-isolation",
    }
    # The orchestrator's bounded sandbox RBAC rides the reconciler-owned
    # namespace — the reconciler creates it, the worker uses it.
    assert cluster.named("upsert_role") == [("hello-saas", "sandbox-provisioner")]
    assert cluster.named("upsert_role_binding") == [("hello-saas", "sandbox-provisioner")]

    # Both bundles under their fixed slots — the names OPA polls; the
    # company's slot never carries the company's name.
    assert set(bundles.stored) == {"company", "platform"}
    # The registry row records what was applied, minus the bundle (which
    # lives in policy_bundles under its own revision) — the policies
    # section included, which the worker reads to say the bounds.
    # The registry stores the compiled spec: neither the bundle nor the
    # blueprint's text, which the console reads back from the CR.
    assert "policyBundle" not in companies.rows["hello-saas"]
    assert "blueprintSource" not in companies.rows["hello-saas"]
    assert companies.rows["hello-saas"]["policies"] == COMPANY_SPEC["policies"]
    assert patch.status["phase"] == "converged"
    assert patch.status["bundleRevision"] == "rev-company"
    # The phase answers for this apply's generation.
    assert patch.status["observedGeneration"] == 1
    # Converged was earned: both revisions were read back from OPA.
    assert sorted(opa.asked) == ["company", "platform"]


def test_company_is_activating_until_opa_serves_its_bundle(cluster, stores, opa):
    """Converged means served. Until OPA reports the stored revision the
    company is not live to the platform — no registry row for the worker
    to start a heartbeat off — and the CR says why."""
    bundles, _instances, companies = stores
    opa.lagging = {"company"}
    patch = kopf.Patch()
    with pytest.raises(kopf.TemporaryError, match="company"):
        reconcile.reconcile_company(
            spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=patch
        )

    # Stored, so the bundle server can hand it to OPA's next poll...
    assert "company" in bundles.stored
    assert patch.status["bundleRevision"] == "rev-company"
    assert patch.status["phase"] == "activating"
    assert patch.status["observedGeneration"] == 1
    # ...but not live.
    assert "hello-saas" not in companies.rows

    # OPA polls; kopf re-runs the handler; now it converges.
    opa.lagging.clear()
    patch = kopf.Patch()
    reconcile.reconcile_company(
        spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=patch
    )
    assert patch.status["phase"] == "converged"
    assert "hello-saas" in companies.rows


def test_a_lagging_platform_bundle_holds_convergence(cluster, stores, opa):
    """The platform's own rules are what the heartbeat's first reads run
    under; a company is not live until those are served either."""
    _bundles, _instances, companies = stores
    opa.lagging = {"platform"}
    with pytest.raises(kopf.TemporaryError, match="platform"):
        reconcile.reconcile_company(
            spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=kopf.Patch()
        )
    assert "hello-saas" not in companies.rows


def test_company_reconcile_is_idempotent(cluster, stores, opa):
    patch = kopf.Patch()
    reconcile.reconcile_company(
        spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=patch
    )
    first = list(cluster.calls)
    reconcile.reconcile_company(
        spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=kopf.Patch()
    )
    assert cluster.calls == first + first  # same intents, no accumulation


def test_company_without_a_bundle_stores_no_company_bundle(cluster, stores, opa):
    bundles, _instances, _companies = stores
    spec = {**COMPANY_SPEC, "policyBundle": {}}
    patch = kopf.Patch()
    reconcile.reconcile_company(spec=spec, name="hello-saas", meta=META, patch=patch)
    assert "company" not in bundles.stored
    assert patch.status["bundleRevision"] == ""
    # Only what was stored is waited for.
    assert opa.asked == ["platform"]


def test_company_teardown_revokes_and_sweeps(cluster, stores, opa):
    bundles, instances, companies = stores
    reconcile.reconcile_company(
        spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=kopf.Patch()
    )
    instances.upsert("pm-1", "hello-saas", "pm", "pm", "standard")
    instances.upsert("pm-9", "other-corp", "pm", "pm", "standard")

    reconcile.teardown_company(spec=COMPANY_SPEC, name="hello-saas")

    # Revoked through policy: the store holds the tombstone OPA polls.
    assert bundles.revoked == ["company"]
    assert bundles.stored["company"] is TOMBSTONE
    # The platform bundle is the platform's, not the company's.
    assert bundles.stored["platform"] is not TOMBSTONE
    assert cluster.named("delete_namespace") == [("hello-saas",)]
    assert "hello-saas" not in companies.rows
    # Swept at the company level — correct even if the reconciler missed
    # individual instance-delete events; other companies untouched.
    assert list(instances.rows) == ["pm-9"]


def test_company_teardown_waits_until_the_revocation_is_served(cluster, stores, opa):
    """Authority first: nothing is torn down while a running OPA still
    serves the company's rules. The delete handler retries until OPA
    reports the tombstone, then sweeps."""
    bundles, instances, companies = stores
    reconcile.reconcile_company(
        spec=COMPANY_SPEC, name="hello-saas", meta=META, patch=kopf.Patch()
    )
    instances.upsert("pm-1", "hello-saas", "pm", "pm", "standard")
    opa.lagging = {"company"}

    with pytest.raises(kopf.TemporaryError, match="still serves"):
        reconcile.teardown_company(spec=COMPANY_SPEC, name="hello-saas")
    assert bundles.stored["company"] is TOMBSTONE  # stored, so OPA can poll it
    assert cluster.named("delete_namespace") == []
    assert "hello-saas" in companies.rows and "pm-1" in instances.rows

    opa.lagging.clear()
    reconcile.teardown_company(spec=COMPANY_SPEC, name="hello-saas")
    assert cluster.named("delete_namespace") == [("hello-saas",)]
    assert "hello-saas" not in companies.rows and "pm-1" not in instances.rows


def test_instance_reconcile_deploys_and_registers(cluster, stores):
    _bundles, instances, _companies = stores
    patch = kopf.Patch()
    reconcile.reconcile_instance(
        spec=INSTANCE_SPEC, namespace="hello-saas", name="pm", patch=patch
    )
    # The workload identity precedes the pod that runs as it.
    assert cluster.named("upsert_service_account") == [("hello-saas", "pm-1")]
    assert cluster.named("upsert_deployment") == [("hello-saas", "pm")]
    assert cluster.named("upsert_service") == [("hello-saas", "pm")]
    assert instances.rows["pm-1"]["status"] == "converged"
    # What the gateway checks a token's namespace against.
    assert instances.rows["pm-1"]["namespace"] == "hello-saas"
    assert patch.status["phase"] == "converged"


def test_instance_teardown_removes_everything(cluster, stores):
    _bundles, instances, _companies = stores
    instances.upsert("pm-1", "hello-saas", "pm", "pm", "standard")
    reconcile.teardown_instance(spec=INSTANCE_SPEC, namespace="hello-saas")
    assert cluster.named("delete_deployment") == [("hello-saas", "pm")]
    assert cluster.named("delete_service") == [("hello-saas", "pm")]
    # Firing revokes: the SA's deletion kills its projected tokens.
    assert cluster.named("delete_service_account") == [("hello-saas", "pm-1")]
    assert instances.rows == {}


def test_startup_keeps_kopf_out_of_status(cluster):
    """The CRDs' structural status schemas prune anything but
    phase/bundleRevision/message, so kopf's bookkeeping must live in
    annotations or the API server silently drops it."""
    settings = kopf.OperatorSettings()
    reconcile.configure(settings=settings)
    assert isinstance(
        settings.persistence.progress_storage, kopf.AnnotationsProgressStorage
    )
    assert isinstance(
        settings.persistence.diffbase_storage, kopf.AnnotationsDiffBaseStorage
    )
