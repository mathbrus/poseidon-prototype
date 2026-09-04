"""The reconcile handlers, as pure orchestration over two seams.

Everything cluster-shaped goes through `kube.Cluster` and everything
database-shaped through the registry accessors; both are replaced with
recording fakes here, so these tests pin *what the operator does* — which
objects it converges, what it sweeps at teardown, what status it writes —
without a cluster or a database.
"""

import kopf
import pytest

from poseidon_operator import reconcile
from poseidon_operator.config import OperatorSettings


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


class FakeBundleStore:
    def __init__(self) -> None:
        self.stored: dict[str, dict] = {}
        self.deleted: list[str] = []

    def put(self, company: str, files: dict) -> str:
        self.stored[company] = files
        return "rev-fixed"

    def delete(self, company: str) -> None:
        self.deleted.append(company)
        self.stored.pop(company, None)


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
        status="ready",
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
        OperatorSettings(
            platform_namespace="poseidon-platform",
            model="mock-brain",
            agent_image="poseidon-agent:abc123",
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


COMPANY_SPEC = {
    "namespace": "hello-saas",
    "vision": "ship it",
    "policyBundle": {"authz.rego": "package poseidon.authz.hello_saas\n"},
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
}

INSTANCE_SPEC = {
    "agentId": "pm-1",
    "company": "hello-saas",
    "standingName": "pm",
    "role": "pm",
    "tools": ["issues.read", "crew.request"],
    "repo": {"url": "github.com/example/repo", "base_ref": "main"},
    "vision": "ship it",
}


def test_company_reconcile_converges_the_envelope(cluster, stores):
    bundles, _instances, companies = stores
    patch = kopf.Patch()
    reconcile.reconcile_company(spec=COMPANY_SPEC, name="hello-saas", patch=patch)

    assert cluster.named("upsert_namespace") == [("hello-saas",)]
    assert {name for ns, name in cluster.named("upsert_network_policy")} == {
        "default-deny",
        "agent-egress",
        "agent-ingress",
        "sandbox-isolation",
    }
    # The orchestrator's bounded sandbox RBAC rides the operator-owned
    # namespace — the operator creates it, the worker uses it.
    assert cluster.named("upsert_role") == [("hello-saas", "sandbox-provisioner")]
    assert cluster.named("upsert_role_binding") == [("hello-saas", "sandbox-provisioner")]

    assert "hello-saas" in bundles.stored
    # The platform scope rides every apply under its reserved key;
    # identical content, so re-applies never churn the revision.
    assert "platform" in bundles.stored
    # The registry row records what was applied, minus the bundle (which
    # lives in policy_bundles under its own revision).
    assert "policyBundle" not in companies.rows["hello-saas"]
    assert patch.status["phase"] == "converged"
    assert patch.status["bundleRevision"] == "rev-fixed"


def test_company_reconcile_is_idempotent(cluster, stores):
    patch = kopf.Patch()
    reconcile.reconcile_company(spec=COMPANY_SPEC, name="hello-saas", patch=patch)
    first = list(cluster.calls)
    reconcile.reconcile_company(spec=COMPANY_SPEC, name="hello-saas", patch=kopf.Patch())
    assert cluster.calls == first + first  # same intents, no accumulation


def test_company_without_a_bundle_stores_no_company_bundle(cluster, stores):
    bundles, _instances, _companies = stores
    spec = {**COMPANY_SPEC, "policyBundle": {}}
    patch = kopf.Patch()
    reconcile.reconcile_company(spec=spec, name="hello-saas", patch=patch)
    assert "hello-saas" not in bundles.stored
    assert patch.status["bundleRevision"] == ""


def test_company_teardown_revokes_and_sweeps(cluster, stores):
    bundles, instances, companies = stores
    reconcile.reconcile_company(spec=COMPANY_SPEC, name="hello-saas", patch=kopf.Patch())
    instances.upsert("pm-1", "hello-saas", "pm", "pm", "standard")
    instances.upsert("pm-9", "other-corp", "pm", "pm", "standard")

    reconcile.teardown_company(spec=COMPANY_SPEC, name="hello-saas")

    assert bundles.deleted == ["hello-saas"]
    # The platform bundle is the platform's, not the company's.
    assert "platform" in bundles.stored
    assert cluster.named("delete_namespace") == [("hello-saas",)]
    assert "hello-saas" not in companies.rows
    # Swept at the company level — correct even if the operator missed
    # individual instance-delete events; other companies untouched.
    assert list(instances.rows) == ["pm-9"]


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
    assert instances.rows["pm-1"]["status"] == "ready"
    # What the gateway checks a token's namespace against.
    assert instances.rows["pm-1"]["namespace"] == "hello-saas"
    assert patch.status["phase"] == "ready"


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
