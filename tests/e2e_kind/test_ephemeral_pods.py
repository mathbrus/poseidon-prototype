"""Ephemeral pods on the live cluster: an ephemeral pod round-trips; RBAC
provably denies pod creation in the platform namespace; and destruction
goes through the {run_id, kind} label selector — an unlabeled pod in the
same namespace survives the sweep.

The real provisioner runs host-side (kubeconfig) with the real agent
image; the pod's containment is probed from inside (the reconciler's agent
envelope covers `ephemeral-agent` pods by kind label); provision/destroy land
process-lifecycle events in the run's story.
"""

import asyncio

from common.audit import LIFECYCLE_DESTROY_POD, LIFECYCLE_PROVISION_POD
from common.ids import new_run_id

from .cluster import (
    COMPANY,
    COMPANY_NS,
    PLATFORM_NS,
    agent_image,
    audit_story,
    crew_activities,
    kubectl,
    probe,
    registry,
    run_kubectl,
    worker_settings,
)

WORKER_SA = f"system:serviceaccount:{PLATFORM_NS}:orchestrator-worker"


def _can_i(verb: str, resource: str, namespace: str) -> bool:
    proc = run_kubectl(
        "auth", "can-i", verb, resource, "-n", namespace, f"--as={WORKER_SA}", timeout=30
    )
    return proc.stdout.strip() == "yes"


def test_worker_rbac_is_bounded(stack: None) -> None:
    """The worker's RBAC boundary, checked against the live API server:
    pods in the company namespace only; never the platform namespace,
    never standing structure."""
    assert _can_i("create", "pods", COMPANY_NS)
    assert _can_i("delete", "pods", COMPANY_NS)
    assert not _can_i("create", "pods", PLATFORM_NS), (
        "the worker can create pods in the platform namespace"
    )
    assert not _can_i("delete", "pods", PLATFORM_NS)
    assert not _can_i("create", "deployments", COMPANY_NS), (
        "the worker can create standing structure"
    )
    assert not _can_i("create", "services", COMPANY_NS)


def test_ephemeral_pod_round_trips_and_destroy_is_label_bounded(
    stack: None, env: dict, tmp_path
) -> None:
    from orchestrator.ephemeral_pods import EphemeralPodProvisioner
    from registry import Runs

    run_id = new_run_id()
    decoy = f"decoy-{run_id[-6:].lower()}"

    with registry() as client:
        crew = crew_activities(
            client, worker_settings(env, tmp_path), EphemeralPodProvisioner(COMPANY_NS)
        )
        Runs(client).start(run_id, "ephemeral pod contract", "implement", None, COMPANY)
        # An unlabeled bystander in the same namespace: the destroy
        # selector must never touch it.
        kubectl(
            "-n",
            COMPANY_NS,
            "run",
            decoy,
            f"--image={agent_image()}",
            "--restart=Never",
            "--command",
            "--",
            "sleep",
            "300",
        )
        try:
            provisioned = asyncio.run(
                crew.provision_ephemeral_pod(
                    {
                        "run_id": run_id,
                        "company": COMPANY,
                        "crew_template": "feature-delivery",
                        "role": "implementer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            pod = provisioned["pod_name"]

            # The pod is Ready (the kubelet's probe hit the agent host) and
            # contained the moment it exists: the reconciler's agent envelope
            # selects the ephemeral-agent kind.
            for url, want in [
                (
                    f"http://llm-gateway.{PLATFORM_NS}.svc.cluster.local:4000/healthz",
                    "reachable",
                ),
                (
                    f"http://mcp-gateway.{PLATFORM_NS}.svc.cluster.local:8080/mcp",
                    "reachable",
                ),
                (f"http://opa.{PLATFORM_NS}.svc.cluster.local:8181/health", "blocked"),
                ("http://api.github.com", "blocked"),
            ]:
                assert probe(pod, url, want), f"{url} should be {want}"

            # Destroy sweeps by {run_id, kind} — the decoy survives.
            deleted = asyncio.run(crew.destroy_ephemeral_pods(run_id))
            assert pod in deleted
            survivor = run_kubectl(
                "-n", COMPANY_NS, "get", "pod", decoy, "-o", "jsonpath={.metadata.name}"
            )
            assert survivor.stdout.strip() == decoy, (
                "the destroy selector deleted an unlabeled pod"
            )

            # Idempotent, and audited per action in the one story.
            assert asyncio.run(crew.destroy_ephemeral_pods(run_id)) == []
            lifecycle = [
                (e["tool"], e["rule"])
                for e in audit_story(run_id)
                if e["source"] == "process-lifecycle"
            ]
            assert (LIFECYCLE_PROVISION_POD, "ephemeral_pod_provisioned") in lifecycle
            assert (LIFECYCLE_DESTROY_POD, "ephemeral_pod_destroyed") in lifecycle
        finally:
            run_kubectl(
                "-n",
                COMPANY_NS,
                "delete",
                "pod",
                decoy,
                "--ignore-not-found",
                "--wait=false",
            )
            asyncio.run(crew.destroy_ephemeral_pods(run_id))
            client.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            client.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
