"""The sandbox backend against the live kind cluster: the k8s_jobs backend
provision/destroy round-trips (Job + toolserver in the company namespace),
and the policy deadline kills a runaway sandbox.

The full sandbox.* proxy path through the gateway is proven by the happy-path suite
(test_happy_path), which runs on this backend end-to-end.

Pod IPs are not routable from the host, so in-pod probes ride the exec API —
the same path the backend itself uses for workspace delivery.
"""

import time
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.contract import SandboxPolicy
from sandbox.k8s_jobs import K8sJobsBackend
from sandbox.manifest import job_name

from .cluster import COMPANY_NS, sandbox_image, sandbox_pods, workspace_tar


@pytest.fixture
def workspace(tmp_path: Path) -> bytes:
    """A git clone as repo.archive would deliver it: base commit + a remote
    to strip."""
    return workspace_tar(tmp_path, remote="https://example.com/repo.git")


@pytest.fixture
def backend(stack: None) -> K8sJobsBackend:
    return K8sJobsBackend(namespace=COMPANY_NS)


def policy(**overrides) -> SandboxPolicy:
    values = {"image": sandbox_image(), "cpu": "500m", "memory": "512Mi"}
    values.update(overrides)
    return SandboxPolicy(**values)


def test_provision_serve_destroy(backend: K8sJobsBackend, workspace: bytes):
    run_id = new_run_id()
    handle = backend.provision(run_id, workspace, policy())
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
                "python",
                "-c",
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


def test_deadline_kills_runaway_sandbox(backend: K8sJobsBackend, workspace: bytes):
    """activeDeadlineSeconds is the wall-clock cap: a sandbox that
    outlives its policy deadline is terminated by the Job controller with no
    orchestrator involvement."""
    run_id = new_run_id()
    # Long enough for provisioning to finish, short enough to observe the
    # kill within the test's polling window.
    handle = backend.provision(run_id, workspace, policy(deadline_s=30))
    try:
        deadline = time.monotonic() + 120
        while sandbox_pods(run_id):
            assert time.monotonic() < deadline, "sandbox pod survived its policy deadline"
            time.sleep(2)
    finally:
        backend.destroy(handle)
