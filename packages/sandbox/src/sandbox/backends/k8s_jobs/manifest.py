"""Job manifest for the k8s_jobs backend — a pure function of the policy.

Everything the SandboxPolicy caps or scopes lands here: resource limits and
requests (disk as ephemeral-storage), the wall-clock deadline as
activeDeadlineSeconds, the workspace scope as the toolserver's WORKSPACE_DIR,
and the per-run env injection seam. Isolation the manifest cannot
express — ingress only from the gateway, zero egress — is the sandbox
NetworkPolicy's job.
"""

from common.podsecurity import CONTAINER_SECURITY_CONTEXT, POD_SECURITY_CONTEXT

from sandbox.contract import SandboxPolicy

RUN_ID_LABEL = "poseidon.io/run-id"
APP_LABEL = "poseidon-sandbox"
TOOLSERVER_PORT = 8000


def job_name(run_id: str) -> str:
    # ULIDs are uppercase; Kubernetes object names must be RFC-1123 lowercase.
    return f"poseidon-sbx-{run_id.lower()}"


def build_job(run_id: str, policy: SandboxPolicy) -> dict:
    labels = {"app": APP_LABEL, RUN_ID_LABEL: run_id}
    resources = {
        "cpu": policy.cpu,
        "memory": policy.memory,
        "ephemeral-storage": policy.disk,
    }
    # python:3.12-slim bakes GPG_KEY (a public release-signing key
    # fingerprint) into the image; blank it so no token-shaped value exists
    # in a company-namespace pod env at all (E3), as the agent does.
    env = {"GPG_KEY": "", "WORKSPACE_DIR": policy.workspace_dir, **policy.env}
    return {
        "apiVersion": "batch/v1",
        "kind": "Job",
        "metadata": {"name": job_name(run_id), "labels": labels},
        "spec": {
            # The toolserver is not a batch workload: it serves until the
            # orchestrator destroys it. A pod that dies mid-run is not
            # restarted — the provision activity's retry re-provisions from
            # scratch instead of resuming into a half-broken workspace.
            "backoffLimit": 0,
            "activeDeadlineSeconds": policy.deadline_s,
            "template": {
                "metadata": {"labels": labels},
                "spec": {
                    "restartPolicy": "Never",
                    # The sandbox holds no credentials and talks to no
                    # Kubernetes API — nothing to mount, nothing to scrape.
                    "automountServiceAccountToken": False,
                    "enableServiceLinks": False,
                    # Non-root (the image's poseidon user owns /workspace),
                    # no privilege-escalation path, no capabilities, default
                    # seccomp filter. The root filesystem stays writable:
                    # workspace code legitimately writes (git worktrees,
                    # pip --user installs) and the ephemeral-storage limit
                    # above caps what it can fill.
                    "securityContext": dict(POD_SECURITY_CONTEXT),
                    "containers": [
                        {
                            "name": "toolserver",
                            "image": policy.image,
                            "imagePullPolicy": "IfNotPresent",
                            "securityContext": dict(CONTAINER_SECURITY_CONTEXT),
                            "env": [
                                {"name": name, "value": value}
                                for name, value in sorted(env.items())
                            ],
                            "ports": [{"containerPort": TOOLSERVER_PORT}],
                            "resources": {
                                "requests": resources,
                                "limits": resources,
                            },
                        }
                    ],
                },
            },
        },
    }
