"""The ephemeral runtime provisioner.

The orchestrator's bounded Kubernetes surface for crew pods — designed,
not improvised: it creates and deletes **pods only**, in the **company
namespace only**, and every delete goes through the
`{run_id, kind=crew-agent}` label selector, so nothing unlabeled (and no
sandbox pod sharing the run label) can ever be touched. Standing structure
stays operator-owned: no Deployments, no Services, no CRs. The matching
Role rules live in `poseidon_operator.resources.sandbox_provisioner_role`
— a review artifact.

A crew pod is the ephemeral executor's brain host: the same agent image a
standing hire runs, labeled to its run, holding its minted identity
token in `GATEWAY_TOKEN` — injected here at provision time, which is what
keeps it out of workflow history. The planner needs
no pod at all (LLM-only); both members share the run's single
sandbox through the gateway.
"""

import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from kubernetes import client as k8s
from kubernetes import config as k8s_config
from kubernetes.client.rest import ApiException

from common.podsecurity import CONTAINER_SECURITY_CONTEXT, POD_SECURITY_CONTEXT
from common.telemetry import get_logger


class CrewPodAbandoned(Exception):
    """The run was torn down while its crew pod was being provisioned, so
    the pod was never created. Not a failure of the run —
    the run is already over."""


AGENT_PORT = 8000
CREW_KIND = "crew-agent"
KIND_LABEL = "poseidon.dev/kind"
RUN_ID_LABEL = "poseidon.io/run-id"
# Wall-clock ceiling on a crew pod, generous enough never to interrupt a
# real dispatch (the orchestrator destroys pods long before this) and short
# enough that an orphaned one stops costing anything the same hour.
CREW_POD_DEADLINE_S = 3600
POD_READY_TIMEOUT_S = 180
DELETE_TIMEOUT_S = 120


def pod_name(agent_id: str) -> str:
    # Lowercased: agent ids embed uppercase ULIDs, pod names are RFC 1123.
    return f"crew-{agent_id}".lower()[:63].rstrip("-")


def crew_pod_manifest(
    *,
    agent_id: str,
    run_id: str,
    company: str,
    agent_image: str,
    env: dict[str, str],
    deadline_s: int = CREW_POD_DEADLINE_S,
) -> dict[str, Any]:
    """The pod, whole and pure (unit-testable without a cluster).

    Mirrors the operator's standing-agent posture: no API-server token, no
    service links, GPG_KEY blanked. The `crew-agent` kind label is what the
    operator's company NetworkPolicies select (gateways-only egress,
    orchestrator-only ingress) and what the destroy selector requires.

    `activeDeadlineSeconds` is the backstop under the teardown race guard,
    the same device sandboxes use: the guard refuses to provision
    for a run whose teardown has passed, but a pod created in the last
    instant before that becomes true has no owner left to collect it. A
    wall-clock deadline means an orphan stops burning compute on its own
    instead of living until someone notices.
    """
    labels = {
        KIND_LABEL: CREW_KIND,
        RUN_ID_LABEL: run_id,
        "poseidon.dev/agent-id": agent_id[:63],
        "poseidon.dev/company": company,
    }
    return {
        "apiVersion": "v1",
        "kind": "Pod",
        "metadata": {"name": pod_name(agent_id), "labels": labels},
        "spec": {
            "restartPolicy": "Never",
            "activeDeadlineSeconds": deadline_s,
            "automountServiceAccountToken": False,
            "enableServiceLinks": False,
            # Non-root, no privilege-escalation path, no capabilities,
            # default seccomp filter. Root filesystem stays writable — the
            # brain SDK writes under $HOME.
            "securityContext": dict(POD_SECURITY_CONTEXT),
            "containers": [
                {
                    "name": "agent",
                    "image": agent_image,
                    "imagePullPolicy": "IfNotPresent",
                    "securityContext": dict(CONTAINER_SECURITY_CONTEXT),
                    "ports": [{"containerPort": AGENT_PORT}],
                    "env": [
                        {"name": name, "value": value}
                        for name, value in sorted({**env, "GPG_KEY": ""}.items())
                    ],
                    "resources": {
                        "requests": {"cpu": "100m", "memory": "256Mi"},
                        "limits": {"cpu": "1", "memory": "1Gi"},
                    },
                    "readinessProbe": {
                        "httpGet": {"path": "/openapi.json", "port": AGENT_PORT},
                        "periodSeconds": 3,
                        "initialDelaySeconds": 2,
                    },
                }
            ],
        },
    }


@dataclass(frozen=True)
class CrewPodHandle:
    agent_id: str
    pod_name: str
    url: str


class CrewPodProvisioner:
    def __init__(self, namespace: str, api_client: k8s.ApiClient | None = None) -> None:
        if api_client is None:
            try:
                k8s_config.load_incluster_config()
            except k8s_config.ConfigException:
                k8s_config.load_kube_config()
            api_client = k8s.ApiClient()
        self._core = k8s.CoreV1Api(api_client)
        self._namespace = namespace
        self._log = get_logger("orchestrator.crewpods")

    def provision(
        self,
        *,
        agent_id: str,
        run_id: str,
        company: str,
        agent_image: str,
        env: dict[str, str],
        still_wanted: Callable[[], bool] | None = None,
    ) -> CrewPodHandle:
        """Create the crew pod and wait until its brain host answers.

        Retry-safe: a remnant pod of the same deterministic agent_id (a
        crashed earlier attempt) is removed first — one member, one pod.

        `still_wanted` is the caller's last word on whether the run this
        pod belongs to is still live; it is consulted immediately before
        creation (see below) and refusing raises `CrewPodAbandoned`.
        """
        name = pod_name(agent_id)
        if self._delete_pod(name):
            self._log.info("removed remnant crew pod", extra={"pod": name})
        # The step-23 guard has to be evaluated *here*, not by the caller
        # before it calls us: removing a remnant waits for that pod to
        # actually terminate, which measured 31 seconds in the failure
        # this closes — ample time for the run's teardown to sweep past a
        # check made beforehand, leaving the pod we are about to create
        # with nothing left to collect it.
        if still_wanted is not None and not still_wanted():
            raise CrewPodAbandoned(
                f"run {run_id} was torn down before {name} could be created"
            )
        manifest = crew_pod_manifest(
            agent_id=agent_id,
            run_id=run_id,
            company=company,
            agent_image=agent_image,
            env=env,
        )
        self._core.create_namespaced_pod(self._namespace, manifest)
        try:
            pod = self._wait_ready(name)
        except Exception:
            self._delete_pod(name)
            raise
        handle = CrewPodHandle(
            agent_id=agent_id,
            pod_name=name,
            url=f"http://{pod.status.pod_ip}:{AGENT_PORT}",
        )
        self._log.info(
            "crew pod provisioned", extra={"run_id": run_id, "url": handle.url}
        )
        return handle

    def destroy(self, run_id: str) -> list[str]:
        """Delete the run's crew pods — by the label selector, never by
        name alone: the bound below is what guarantees nothing unlabeled
        (and nothing of another run) can be deleted through this surface."""
        selector = f"{KIND_LABEL}={CREW_KIND},{RUN_ID_LABEL}={run_id}"
        pods = self._core.list_namespaced_pod(
            self._namespace, label_selector=selector
        )
        deleted = []
        for pod in pods.items:
            if self._delete_pod(pod.metadata.name, wait=True):
                deleted.append(pod.metadata.name)
        if deleted:
            self._log.info(
                "crew pods destroyed", extra={"run_id": run_id, "pods": deleted}
            )
        return deleted

    # -- plumbing -----------------------------------------------------------

    def _delete_pod(self, name: str, wait: bool = True) -> bool:
        try:
            self._core.delete_namespaced_pod(name, self._namespace)
        except ApiException as exc:
            if exc.status == 404:
                return False
            raise
        if wait:
            deadline = time.monotonic() + DELETE_TIMEOUT_S
            while True:
                try:
                    self._core.read_namespaced_pod(name, self._namespace)
                except ApiException as exc:
                    if exc.status == 404:
                        break
                    raise
                if time.monotonic() > deadline:
                    raise RuntimeError(f"pod {name} not deleted after {DELETE_TIMEOUT_S}s")
                time.sleep(1)
        return True

    def _wait_ready(self, name: str):
        deadline = time.monotonic() + POD_READY_TIMEOUT_S
        while True:
            pod = self._core.read_namespaced_pod(name, self._namespace)
            phase = (pod.status and pod.status.phase) or ""
            if phase in ("Failed", "Succeeded"):
                raise RuntimeError(f"crew pod {name} ended in phase {phase}")
            ready = any(
                c.type == "Ready" and c.status == "True"
                for c in (pod.status.conditions or [])
            )
            if ready and pod.status.pod_ip:
                return pod
            if time.monotonic() > deadline:
                raise RuntimeError(
                    f"crew pod {name} not ready after {POD_READY_TIMEOUT_S}s"
                )
            time.sleep(1)
