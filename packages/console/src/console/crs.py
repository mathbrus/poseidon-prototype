"""Turning a deployment plan into custom resources.

The only place the console touches a cluster, and it touches nothing but
the two CRDs: every real object — namespace, NetworkPolicy, Deployment, bundle — is
the reconciler's to create from these. Submitting a CR is a statement of
intent, never an instruction.

Instances the plan no longer contains are deleted, which is what makes
firing a blueprint diff rather than an ops task.
"""

import time
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config

from common.topology import TEARDOWN_TIMEOUT_S
from compiler import DeploymentPlan

GROUP = "poseidon.dev"
VERSION = "v1"
NAMESPACE_TIMEOUT_S = 120.0


def _api() -> k8s.CustomObjectsApi:
    try:
        k8s_config.load_incluster_config()
    except k8s_config.ConfigException:
        k8s_config.load_kube_config()
    return k8s.CustomObjectsApi()


def company_cr(plan: DeploymentPlan) -> dict[str, Any]:
    data = plan.as_dict()
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "Company",
        "metadata": {"name": plan.company.name},
        "spec": {
            "namespace": plan.company.namespace,
            "vision": plan.company.vision,
            "repo": {
                "url": plan.company.repo.url,
                "base_ref": plan.company.repo.base_ref,
            },
            "documents": dict(plan.company.documents),
            # The policies section as written, beside the bundle that
            # enforces it: what the platform reads to say the bounds and
            # to compose the contract-verify baseline.
            "policies": plan.company.policies,
            "policyBundle": plan.bundle.files,
            # Compiler-owned and identical from every blueprint; riding the
            # CR keeps the reconciler the only bundle writer. Every
            # apply upserts the same revision, so it never churns.
            "platformBundle": plan.platform_bundle.files,
            "infra": data["infra"],
            "workflows": data["workflows"],
            "budgets": data["budgets"],
            "ephemeralAgents": data["ephemeral_agents"],
            "heartbeat": data["heartbeat"],
        },
    }


def instance_cr(plan: DeploymentPlan, instance) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "AgentInstance",
        "metadata": {"name": instance.standing_name, "namespace": plan.company.namespace},
        "spec": {
            "agentId": instance.agent_id,
            "company": instance.company,
            "standingName": instance.standing_name,
            "role": instance.role,
            "tools": list(instance.tools),
            "repo": {"url": instance.repo.url, "base_ref": instance.repo.base_ref},
            "vision": instance.vision,
            "maySpawn": list(instance.may_spawn),
        },
    }


def submit(
    plan: DeploymentPlan, api: k8s.CustomObjectsApi | None = None
) -> tuple[int, int, int]:
    """Submit the plan's CRs. Returns what was created, what was updated,
    and the Company's generation this apply made — the number the phase
    a reader waits for must answer for."""
    api = api or _api()
    created = updated = 0

    existed, generation = _replace_cluster(
        api, "companies", plan.company.name, company_cr(plan)
    )
    if existed:
        updated += 1
    else:
        created += 1

    # The Company reconcile creates the namespace; instances land in it —
    # so wait for the namespace, bounded, before submitting them. On an
    # update the namespace already exists and this is a single read.
    namespace = plan.company.namespace
    _wait_for_namespace(namespace)
    planned = set()
    for instance in plan.instances:
        planned.add(instance.standing_name)
        if _replace_namespaced(
            api,
            namespace,
            "agentinstances",
            instance.standing_name,
            instance_cr(plan, instance),
        ):
            updated += 1
        else:
            created += 1

    for standing_name in _existing_instances(api, namespace) - planned:
        api.delete_namespaced_custom_object(
            GROUP, VERSION, namespace, "agentinstances", standing_name
        )

    return created, updated, generation


def read_company(api: k8s.CustomObjectsApi | None = None) -> dict[str, Any] | None:
    """The one Company CR, as a reader of its phase sees it, or None when
    none is applied. Read from the cluster, not the registry: the registry
    row exists only once the company is converged, and the phase is what
    says whether it is."""
    api = api or _api()
    items = api.list_cluster_custom_object(GROUP, VERSION, "companies").get("items", [])
    if not items:
        return None
    if len(items) > 1:
        names = sorted(item["metadata"]["name"] for item in items)
        raise RuntimeError(f"more than one company is applied: {names}")
    item = items[0]
    status = item.get("status") or {}
    return {
        "company": item["metadata"]["name"],
        "namespace": item["spec"]["namespace"],
        "generation": item["metadata"].get("generation", 0),
        "phase": status.get("phase") or "activating",
        "observedGeneration": status.get("observedGeneration", 0),
        "bundleRevision": status.get("bundleRevision", ""),
        "message": status.get("message", ""),
    }


def delete_company(
    name: str,
    wait: bool = True,
    api: k8s.CustomObjectsApi | None = None,
    timeout_s: float = TEARDOWN_TIMEOUT_S,
) -> None:
    """Delete the Company CR and, unless told not to, wait for the
    reconciler's teardown to finish — the CR is gone once the tombstone is
    served, the rows swept and the namespace deleted. A company that is
    not there is already deleted."""
    api = api or _api()
    try:
        api.delete_cluster_custom_object(GROUP, VERSION, "companies", name)
    except k8s.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        return
    if not wait:
        return
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            api.get_cluster_custom_object(GROUP, VERSION, "companies", name)
        except k8s.exceptions.ApiException as exc:
            if exc.status == 404:
                return
            raise
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"company {name!r} is still being torn down after {timeout_s:.0f}s"
                " — see its status, and the poseidon-reconciler's logs"
            )
        time.sleep(2)


def _wait_for_namespace(namespace: str, timeout_s: float = NAMESPACE_TIMEOUT_S) -> None:
    """Wait for the reconciler to create the company namespace. Waiting on the
    namespace rather than Company status keeps this decoupled from kopf's
    status mechanics: the namespace is what the AgentInstance create below
    actually needs."""
    core = k8s.CoreV1Api()
    deadline = time.monotonic() + timeout_s
    while True:
        try:
            if core.read_namespace(namespace).status.phase == "Active":
                return
        except k8s.exceptions.ApiException as exc:
            if exc.status != 404:
                raise
        if time.monotonic() >= deadline:
            raise TimeoutError(
                f"namespace {namespace!r} did not appear within {timeout_s:.0f}s"
                " — is the poseidon-reconciler running and reconciling the"
                " Company CR?"
            )
        time.sleep(2)


def _replace_cluster(
    api: k8s.CustomObjectsApi, plural: str, name: str, body: dict[str, Any]
) -> tuple[bool, int]:
    """Create or replace; whether it existed, and the generation the
    write produced (a replace that changes nothing keeps the old one)."""
    try:
        existing = api.get_cluster_custom_object(GROUP, VERSION, plural, name)
    except k8s.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        made = api.create_cluster_custom_object(GROUP, VERSION, plural, body)
        return False, int((made.get("metadata") or {}).get("generation", 1))
    body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    made = api.replace_cluster_custom_object(GROUP, VERSION, plural, name, body)
    return True, int((made.get("metadata") or {}).get("generation", 0))


def _replace_namespaced(
    api: k8s.CustomObjectsApi,
    namespace: str,
    plural: str,
    name: str,
    body: dict[str, Any],
) -> bool:
    try:
        existing = api.get_namespaced_custom_object(
            GROUP, VERSION, namespace, plural, name
        )
    except k8s.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        api.create_namespaced_custom_object(GROUP, VERSION, namespace, plural, body)
        return False
    body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    api.replace_namespaced_custom_object(GROUP, VERSION, namespace, plural, name, body)
    return True


def _existing_instances(api: k8s.CustomObjectsApi, namespace: str) -> set[str]:
    try:
        listing = api.list_namespaced_custom_object(
            GROUP, VERSION, namespace, "agentinstances"
        )
    except k8s.exceptions.ApiException as exc:
        if exc.status in (403, 404):
            return set()
        raise
    return {item["metadata"]["name"] for item in listing.get("items", [])}
