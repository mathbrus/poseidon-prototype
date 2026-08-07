"""Turning a deployment plan into custom resources.

The only place the CLI touches a cluster, and it touches nothing but the two
CRDs: every real object — namespace, NetworkPolicy, Deployment, bundle — is
the operator's to create from these. Submitting a CR is a statement of
intent, never an instruction.

Instances the plan no longer contains are deleted, which is what makes
firing a blueprint diff rather than an ops task.
"""

import time
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config

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
            "mission": plan.company.mission,
            "stage": plan.company.stage or "",
            "policyBundle": plan.bundle.files,
            # Compiler-owned and identical from every blueprint; riding the
            # CR keeps the operator the only bundle writer. Every
            # apply upserts the same revision, so it never churns.
            "platformBundle": plan.platform_bundle.files,
            "infra": data["infra"],
            "workflows": data["workflows"],
            "budgets": data["budgets"],
            "crewMembers": data["crew_members"],
        },
    }


def instance_cr(plan: DeploymentPlan, instance) -> dict[str, Any]:
    return {
        "apiVersion": f"{GROUP}/{VERSION}",
        "kind": "AgentInstance",
        "metadata": {"name": instance.hire, "namespace": plan.company.namespace},
        "spec": {
            "agentId": instance.agent_id,
            "company": instance.company,
            "hire": instance.hire,
            "roleTemplate": instance.role_template,
            "modelTier": instance.model_tier,
            "tools": list(instance.tools),
            "maySpawn": list(instance.may_spawn),
            **(
                {"maxConcurrentSpawns": instance.max_concurrent_spawns}
                if instance.max_concurrent_spawns
                else {}
            ),
            "runtimeHints": dict(instance.runtime_hints),
        },
    }


def submit(plan: DeploymentPlan, api: k8s.CustomObjectsApi | None = None) -> tuple[int, int]:
    api = api or _api()
    created = updated = 0

    if _replace_cluster(api, "companies", plan.company.name, company_cr(plan)):
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
        planned.add(instance.hire)
        if _replace_namespaced(
            api, namespace, "agentinstances", instance.hire, instance_cr(plan, instance)
        ):
            updated += 1
        else:
            created += 1

    for hire in _existing_instances(api, namespace) - planned:
        api.delete_namespaced_custom_object(
            GROUP, VERSION, namespace, "agentinstances", hire
        )

    return created, updated


def _wait_for_namespace(
    namespace: str, timeout_s: float = NAMESPACE_TIMEOUT_S
) -> None:
    """Wait for the operator to create the company namespace. Waiting on the
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
                " — is the poseidon-operator running and reconciling the"
                " Company CR?"
            )
        time.sleep(2)


def _replace_cluster(
    api: k8s.CustomObjectsApi, plural: str, name: str, body: dict[str, Any]
) -> bool:
    try:
        existing = api.get_cluster_custom_object(GROUP, VERSION, plural, name)
    except k8s.exceptions.ApiException as exc:
        if exc.status != 404:
            raise
        api.create_cluster_custom_object(GROUP, VERSION, plural, body)
        return False
    body["metadata"]["resourceVersion"] = existing["metadata"]["resourceVersion"]
    api.replace_cluster_custom_object(GROUP, VERSION, plural, name, body)
    return True


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
    api.replace_namespaced_custom_object(
        GROUP, VERSION, namespace, plural, name, body
    )
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
