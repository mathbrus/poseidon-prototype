"""Unit tests for the k8s_jobs Job manifest: every SandboxPolicy cap and
scope must land in the spec (the statically checkable half;
the live kill-a-runaway proof is the e2e_kind suite)."""

from sandbox.contract import SandboxPolicy
from sandbox.manifest import RUN_ID_LABEL, build_job, job_name

RUN_ID = "01JTESTULID0000000000000AB"  # ULIDs are uppercase

POLICY = SandboxPolicy(
    image="poseidon-sandbox:test",
    workspace_dir="/workspace",
    cpu="750m",
    memory="512Mi",
    disk="1Gi",
    deadline_s=1234,
    env={"EGRESS_PROXY_CREDENTIAL": "per-run-secret"},
)


def test_job_name_is_rfc1123():
    name = job_name(RUN_ID)
    assert name == f"poseidon-sbx-{RUN_ID.lower()}"
    assert name == name.lower()


def container(job: dict) -> dict:
    return job["spec"]["template"]["spec"]["containers"][0]


def test_policy_caps_and_deadline_land_in_the_spec():
    job = build_job(RUN_ID, POLICY)
    assert job["spec"]["activeDeadlineSeconds"] == 1234
    resources = container(job)["resources"]
    for section in ("requests", "limits"):
        assert resources[section] == {
            "cpu": "750m",
            "memory": "512Mi",
            "ephemeral-storage": "1Gi",
        }


def test_run_id_label_on_job_and_pod():
    job = build_job(RUN_ID, POLICY)
    assert job["metadata"]["labels"][RUN_ID_LABEL] == RUN_ID
    pod_labels = job["spec"]["template"]["metadata"]["labels"]
    assert pod_labels[RUN_ID_LABEL] == RUN_ID
    assert pod_labels["app"] == "poseidon-sandbox"


def test_workspace_scope_and_env_injection_seam():
    job = build_job(RUN_ID, POLICY)
    env = {e["name"]: e["value"] for e in container(job)["env"]}
    assert env["WORKSPACE_DIR"] == "/workspace"
    # The env injection point: per-run values reach the pod env.
    assert env["EGRESS_PROXY_CREDENTIAL"] == "per-run-secret"


def test_base_image_gpg_key_is_blanked():
    """No token-shaped value in a company-namespace pod env."""
    env = {e["name"]: e["value"] for e in container(build_job(RUN_ID, POLICY))["env"]}
    assert env["GPG_KEY"] == ""


def test_sandbox_holds_no_kubernetes_identity():
    job = build_job(RUN_ID, POLICY)
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["automountServiceAccountToken"] is False
    assert pod_spec["restartPolicy"] == "Never"
    assert job["spec"]["backoffLimit"] == 0


def test_sandbox_runs_non_root_with_no_escalation_path():
    """The pod runs as the image's non-root user, with no privilege
    escalation, no capabilities and the default seccomp filter. The root
    filesystem stays writable on purpose (workspace code writes); the
    ephemeral-storage limit caps what it can fill."""
    job = build_job(RUN_ID, POLICY)
    pod_spec = job["spec"]["template"]["spec"]
    assert pod_spec["securityContext"] == {
        "runAsNonRoot": True,
        "runAsUser": 10001,
        "runAsGroup": 10001,
        "seccompProfile": {"type": "RuntimeDefault"},
    }
    assert container(job)["securityContext"] == {
        "allowPrivilegeEscalation": False,
        "capabilities": {"drop": ["ALL"]},
    }
