"""Step-4 exit criteria against the live kind cluster: the k8s_jobs backend
provision/destroy round-trips (Job + toolserver in the company namespace),
and the policy deadline kills a runaway sandbox (§4.6).

The full sandbox.* proxy path through the gateway is proven by the E1 suite
(test_happy_path), which now runs on this backend end-to-end.

Pod IPs are not routable from the host, so in-pod probes ride the exec API —
the same path the backend itself uses for workspace delivery.
"""

import io
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.backends.k8s_jobs import K8sJobsBackend
from sandbox.backends.k8s_jobs.manifest import job_name
from sandbox.contract import SandboxPolicy

from .conftest import COMPANY_NS, sandbox_image, sandbox_pods

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",
}


def git(workspace: Path, *args: str) -> str:
    proc = subprocess.run(
        ["git", *args], cwd=workspace, env=GIT_ENV, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


@pytest.fixture
def workspace_tar(tmp_path: Path) -> bytes:
    """A git clone as repo.archive would deliver it: base commit + a remote
    to strip."""
    git(tmp_path, "init", "-b", "main")
    (tmp_path / "hello.txt").write_text("hello\n")
    git(tmp_path, "add", "-A")
    git(tmp_path, "commit", "-m", "base")
    git(tmp_path, "remote", "add", "origin", "https://example.com/repo.git")
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in tmp_path.iterdir():
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


@pytest.fixture
def backend(stack: None) -> K8sJobsBackend:
    return K8sJobsBackend(namespace=COMPANY_NS)


def policy(**overrides) -> SandboxPolicy:
    values = {"image": sandbox_image(), "cpu": "500m", "memory": "512Mi"}
    values.update(overrides)
    return SandboxPolicy(**values)


def test_provision_serve_destroy(backend: K8sJobsBackend, workspace_tar: bytes):
    run_id = new_run_id()
    handle = backend.provision(run_id, workspace_tar, policy())
    try:
        assert handle.run_id == run_id
        assert handle.namespace == COMPANY_NS
        assert handle.job_name == job_name(run_id)
        assert sandbox_pods(run_id) == [handle.pod_name]

        # The workspace arrived, remotes are stripped, the base ref is
        # recorded, and the toolserver answers — probed in-pod.
        exit_code, output = backend._exec(
            handle.pod_name,
            ["sh", "-c", "cd /workspace && ls -a && git remote && cat .poseidon-base"],
        )
        assert exit_code == 0, output
        assert "hello.txt" in output
        assert ".poseidon-base" in output
        exit_code, output = backend._exec(
            handle.pod_name,
            [
                "python", "-c",
                "import urllib.request; print(urllib.request.urlopen("
                "'http://localhost:8000/openapi.json', timeout=2).status)",
            ],
        )
        assert exit_code == 0, output
        assert "200" in output
    finally:
        backend.destroy(handle)

    assert sandbox_pods(run_id) == []
    backend.destroy(handle)  # idempotent


def test_deadline_kills_runaway_sandbox(backend: K8sJobsBackend, workspace_tar: bytes):
    """activeDeadlineSeconds is the wall-clock cap of §4.6: a sandbox that
    outlives its policy deadline is terminated by the Job controller with no
    orchestrator involvement."""
    run_id = new_run_id()
    # Long enough for provisioning to finish, short enough to observe the
    # kill within the test's polling window.
    handle = backend.provision(run_id, workspace_tar, policy(deadline_s=30))
    try:
        deadline = time.monotonic() + 120
        while sandbox_pods(run_id):
            assert time.monotonic() < deadline, (
                "sandbox pod survived its policy deadline"
            )
            time.sleep(2)
    finally:
        backend.destroy(handle)
