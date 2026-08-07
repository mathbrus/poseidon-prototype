"""Step 16c done-when, on the live cluster: a crew pod round-trips; RBAC
provably denies pod creation in the platform namespace; and destruction
goes through the {run_id, kind} label selector — an unlabeled pod in the
same namespace survives the sweep.

The real provisioner runs host-side (kubeconfig) with the real agent
image; the pod's containment is probed from inside (the operator's agent
envelope covers `crew-agent` pods by kind label); provision/destroy land
process-lifecycle events in the run's story.
"""

import asyncio
import subprocess

from common.ids import new_run_id

from .conftest import COMPANY_NS, PLATFORM_NS, audit_story
from .test_crew_identity import _cluster_secret, _registry

WORKER_SA = f"system:serviceaccount:{PLATFORM_NS}:orchestrator-worker"

_PROBE = """
import sys, urllib.request, urllib.error
url, want = sys.argv[1], sys.argv[2]
try:
    urllib.request.urlopen(url, timeout=3)
    reachable = True
except urllib.error.HTTPError:
    reachable = True
except Exception:
    reachable = False
sys.exit(0 if reachable == (want == "reachable") else 1)
"""


def _can_i(verb: str, resource: str, namespace: str) -> bool:
    proc = subprocess.run(
        [
            "kubectl",
            "auth",
            "can-i",
            verb,
            resource,
            "-n",
            namespace,
            f"--as={WORKER_SA}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    return proc.stdout.strip() == "yes"


def agent_image() -> str:
    proc = subprocess.run(
        [
            "kubectl",
            "-n",
            PLATFORM_NS,
            "get",
            "deployment/orchestrator-worker",
            "-o",
            'jsonpath={.spec.template.spec.containers[0].env[?(@.name=="AGENT_IMAGE")].value}',
        ],
        capture_output=True,
        text=True,
        timeout=30,
        check=True,
    )
    assert proc.stdout.strip(), "worker carries no AGENT_IMAGE env"
    return proc.stdout.strip()


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


def test_crew_pod_round_trips_and_destroy_is_label_bounded(
    stack: None, env: dict, tmp_path
) -> None:
    from orchestrator.activities.crew import CrewActivities
    from orchestrator.config import WorkerSettings
    from orchestrator.crewpods import CrewPodProvisioner
    from registry import AuditStore, RunParticipants, Runs

    run_id = new_run_id()
    token_file = tmp_path / "unused"
    token_file.write_text("unused")
    decoy = f"decoy-{run_id[-6:].lower()}"

    with _registry() as registry:
        settings = WorkerSettings(
            gateway_token_file=str(token_file),
            mcp_gateway_url=env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080"),
            target_repo=env.get("TARGET_REPO", "github.com/example/repo"),
            platform_jwt_key=_cluster_secret("platform-jwt-key", "PLATFORM_JWT_KEY"),
            agent_image=agent_image(),
            model="mock-brain",
        )
        crew = CrewActivities(
            settings,
            RunParticipants(registry),
            AuditStore(registry),
            CrewPodProvisioner(COMPANY_NS),
        )
        Runs(registry).start(run_id, "16c pod contract", "implement", None, "hello-saas")
        # An unlabeled bystander in the same namespace: the destroy
        # selector must never touch it.
        subprocess.run(
            [
                "kubectl",
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
            ],
            check=True,
            capture_output=True,
            timeout=60,
        )
        try:
            provisioned = asyncio.run(
                crew.provision_crew_pod(
                    {
                        "run_id": run_id,
                        "company": "hello-saas",
                        "crew_template": "feature-delivery",
                        "member": "executor",
                        "role_template": "backend-engineer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            pod = provisioned["pod_name"]

            # The pod is Ready (the kubelet's probe hit the brain host) and
            # contained the moment it exists: the operator's agent envelope
            # selects the crew-agent kind.
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
                probe = subprocess.run(
                    [
                        "kubectl",
                        "-n",
                        COMPANY_NS,
                        "exec",
                        pod,
                        "--",
                        "python",
                        "-c",
                        _PROBE,
                        url,
                        want,
                    ],
                    capture_output=True,
                    text=True,
                    timeout=60,
                )
                assert probe.returncode == 0, f"{url} should be {want}"

            # Destroy sweeps by {run_id, kind} — the decoy survives.
            deleted = asyncio.run(crew.destroy_crew_pods(run_id))
            assert pod in deleted
            survivor = subprocess.run(
                [
                    "kubectl",
                    "-n",
                    COMPANY_NS,
                    "get",
                    "pod",
                    decoy,
                    "-o",
                    "jsonpath={.metadata.name}",
                ],
                capture_output=True,
                text=True,
                timeout=30,
            )
            assert survivor.stdout.strip() == decoy, (
                "the destroy selector deleted an unlabeled pod"
            )

            # Idempotent, and audited per action in the one story.
            assert asyncio.run(crew.destroy_crew_pods(run_id)) == []
            lifecycle = [
                (e["tool"], e["rule"])
                for e in audit_story(run_id)
                if e["source"] == "process-lifecycle"
            ]
            assert ("lifecycle.provision_pod", "crew_pod_provisioned") in lifecycle
            assert ("lifecycle.destroy_pod", "crew_pod_destroyed") in lifecycle
        finally:
            subprocess.run(
                [
                    "kubectl",
                    "-n",
                    COMPANY_NS,
                    "delete",
                    "pod",
                    decoy,
                    "--ignore-not-found",
                    "--wait=false",
                ],
                capture_output=True,
                timeout=60,
            )
            asyncio.run(crew.destroy_crew_pods(run_id))
            registry.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            registry.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
