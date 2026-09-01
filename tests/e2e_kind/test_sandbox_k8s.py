"""The one sandbox claim only infrastructure can settle: the policy
deadline kills a runaway sandbox with no orchestrator involvement.

The provision/serve/destroy round-trip on this same backend is proven
end-to-end by the happy-path suite (test_happy_path).
"""

import time
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.contract import SandboxPolicy
from sandbox.k8s_jobs import K8sJobsBackend

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
