"""kopf handlers: converge Company and AgentInstance CRs.

The operator owns standing structure and nothing else. It creates
namespaces, the NetworkPolicy envelope, the orchestrator's bounded
sandbox-provisioner RBAC, standing agent Deployments, and it persists
compiled bundles for the bundle server to publish. It never creates
ephemeral agents (the orchestrator's fulfill activities do, inside a run),
never runs a workflow, and never mints a credential.

Reconciliation is idempotent by construction: every object is sent whole
and upserted (see `kube.Cluster`), so a restart, a resync or a repeated
apply converges to the same state rather than accumulating.
"""

from typing import Any

import kopf

from common.telemetry import get_logger
from poseidon_operator import resources
from poseidon_operator.config import OperatorSettings
from poseidon_operator.kube import Cluster
from poseidon_operator.opa import activated_revision
from registry import AgentInstances, Companies, PolicyBundles, open_registry

# How long a handler waits before asking OPA again whether it has
# activated a stored bundle. OPA polls the bundle server every few
# seconds; asking more often than that buys nothing.
ACTIVATION_POLL_S = 5

_log = get_logger("operator")
_registry = None
_the_cluster = None
_the_settings = None


def _cluster() -> Cluster:
    global _the_cluster
    if _the_cluster is None:
        _the_cluster = Cluster()
    return _the_cluster


def _db():
    global _registry
    if _registry is None:
        _registry = open_registry()
    return _registry


def _settings() -> OperatorSettings:
    global _the_settings
    if _the_settings is None:
        _the_settings = OperatorSettings()
    return _the_settings


@kopf.on.startup()
def configure(settings: kopf.OperatorSettings, **_: Any) -> None:
    """Keep kopf's bookkeeping in annotations. The CRDs declare structural
    status schemas (only phase/bundleRevision/message survive pruning), so
    the default status-based progress storage would be silently dropped by
    the API server and warn about patch inconsistencies on every event."""
    settings.persistence.progress_storage = kopf.AnnotationsProgressStorage(
        prefix="poseidon.dev"
    )
    settings.persistence.diffbase_storage = kopf.AnnotationsDiffBaseStorage(
        prefix="poseidon.dev", key="last-handled-configuration"
    )


# -- Company ---------------------------------------------------------------


@kopf.on.create("poseidon.dev", "v1", "companies")
@kopf.on.update("poseidon.dev", "v1", "companies")
@kopf.on.resume("poseidon.dev", "v1", "companies")
def reconcile_company(
    spec: dict[str, Any], name: str, meta: dict[str, Any], patch: kopf.Patch, **_: Any
) -> None:
    cluster = _cluster()
    company_ns = spec["namespace"]
    platform_ns = _settings().platform_namespace

    cluster.upsert_namespace(resources.namespace(company_ns))
    for policy in (
        resources.default_deny(company_ns),
        resources.agent_egress(company_ns, platform_ns),
        resources.agent_ingress(company_ns, platform_ns),
        # The sandbox envelope comes from the compiled infra IR —
        # hand-authoring a projection is a bug.
        resources.sandbox_isolation(
            company_ns, platform_ns, (spec.get("infra") or {}).get("network_policy")
        ),
    ):
        cluster.upsert_network_policy(company_ns, policy)

    # The orchestrator's bounded sandbox surface rides the namespace: the
    # namespace is operator-created, so the Role in it must be too (the
    # operator's ClusterRole holds escalate/bind for exactly this — it still
    # cannot touch a pod itself).
    cluster.upsert_role(company_ns, resources.sandbox_provisioner_role(company_ns))
    cluster.upsert_role_binding(
        company_ns, resources.sandbox_provisioner_binding(company_ns, platform_ns)
    )

    revision = ""
    stored: dict[str, str] = {}  # bundle name -> the revision persisted
    bundle = spec.get("policyBundle") or {}
    if bundle:
        # Persist, do not push: OPA polls the bundle server for this
        # revision. Nothing installs policy into a running OPA.
        revision = stored[name] = PolicyBundles(_db()).put(name, dict(bundle))
        _log.info("policy bundle stored", extra={"company": name, "revision": revision})
    platform_bundle = spec.get("platformBundle") or {}
    if platform_bundle:
        # Reserved key, never a company name (the compiler refuses one).
        # Content-addressed revision: identical from every apply, so this
        # is a no-op except on a compiler upgrade.
        stored["platform"] = PolicyBundles(_db()).put("platform", dict(platform_bundle))

    # Explicit status writes: kopf handler return values land under
    # status.<handler_id>, which the structural schema prunes. The
    # generation says which apply the phase below answers for — a reader
    # of a re-applied company must not take the previous apply's
    # `converged` for this one's.
    patch.status["bundleRevision"] = revision
    patch.status["observedGeneration"] = meta["generation"]

    # Converged means served. The registry row is what makes the company
    # live to the platform — the worker starts its heartbeat off it, the
    # gateways read it — so it is written only once OPA has activated
    # exactly the revisions stored above. OPA polls on its own schedule;
    # until it reports them the CR says `activating` and kopf re-runs this
    # handler, which is idempotent by construction.
    opa_url = _settings().opa_url
    lagging = sorted(
        bundle_name
        for bundle_name, stored_revision in stored.items()
        if activated_revision(opa_url, bundle_name) != stored_revision
    )
    if lagging:
        patch.status["phase"] = "activating"
        raise kopf.TemporaryError(
            f"OPA has not activated {', '.join(lagging)} yet", delay=ACTIVATION_POLL_S
        )

    Companies(_db()).upsert(name, {k: v for k, v in spec.items() if k != "policyBundle"})
    patch.status["phase"] = "converged"


@kopf.on.delete("poseidon.dev", "v1", "companies")
def teardown_company(spec: dict[str, Any], name: str, **_: Any) -> None:
    """Teardown revokes, authority first. The stored bundle becomes a
    tombstone over the company's roots, and nothing is torn down until OPA
    serves it: a running OPA drops the company's rules on its own next
    poll, no restart, so a caller still holding an identity finds no rule
    to match before its namespace goes. The platform bundle stays — it
    belongs to the platform, not to any company. Registry rows are swept
    at the company level — correct even if the operator missed individual
    instance-delete events."""
    revoked = PolicyBundles(_db()).revoke(name)
    if revoked and activated_revision(_settings().opa_url, name) != revoked:
        raise kopf.TemporaryError(
            f"OPA still serves policy for {name}", delay=ACTIVATION_POLL_S
        )
    AgentInstances(_db()).delete_for_company(name)
    Companies(_db()).delete(name)
    _cluster().delete_namespace(spec["namespace"])
    _log.info("company torn down", extra={"company": name})


# -- AgentInstance ---------------------------------------------------------


@kopf.on.create("poseidon.dev", "v1", "agentinstances")
@kopf.on.update("poseidon.dev", "v1", "agentinstances")
@kopf.on.resume("poseidon.dev", "v1", "agentinstances")
def reconcile_instance(
    spec: dict[str, Any], namespace: str, name: str, patch: kopf.Patch, **_: Any
) -> None:
    cluster = _cluster()
    # The ServiceAccount precedes the Deployment that runs as it; its name
    # (the agent_id) plus the namespace recorded below are what the gateway
    # verifies a projected token against.
    cluster.upsert_service_account(
        namespace, resources.agent_service_account(spec, namespace)
    )
    cluster.upsert_deployment(
        namespace, resources.agent_deployment(spec, namespace, _settings())
    )
    cluster.upsert_service(namespace, resources.agent_service(spec, namespace))

    AgentInstances(_db()).upsert(
        agent_id=spec["agentId"],
        company=spec["company"],
        standing_name=spec["standingName"],
        role=spec["role"],
        namespace=namespace,
    )
    _log.info("instance converged", extra={"agent_id": spec["agentId"]})
    patch.status["phase"] = "ready"


@kopf.on.delete("poseidon.dev", "v1", "agentinstances")
def teardown_instance(spec: dict[str, Any], namespace: str, **_: Any) -> None:
    """The firing half of hire/fire-as-diff.
    Deleting the ServiceAccount revokes the instance's workload identity:
    the API server refuses TokenReview for tokens of an SA that no longer
    exists, so a fired agent's projected token dies with it (bounded by the
    gateway's authn cache TTL)."""
    cluster = _cluster()
    cluster.delete_deployment(namespace, spec["standingName"])
    cluster.delete_service(namespace, spec["standingName"])
    cluster.delete_service_account(namespace, spec["agentId"])
    AgentInstances(_db()).delete(spec["agentId"])
    _log.info("instance removed", extra={"agent_id": spec["agentId"]})
