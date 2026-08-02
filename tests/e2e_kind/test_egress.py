"""Step-5 exit criteria against the live kind cluster (§4.6): from inside a
sandbox, a request to a non-allowlisted host is blocked *and* the deny lands
in the registry audit store correlated by run_id; an allowlisted `pip install`
succeeds through the proxy; and the NetworkPolicy backstop silently drops
attempts to bypass the proxy.

The sandbox is provisioned exactly as the orchestrator does it: the per-run
proxy credential is minted with the shared key (read from the cluster
secret) and injected via `SandboxPolicy.env`.
"""

import io
import subprocess
import tarfile
import time
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.backends.k8s_jobs import K8sJobsBackend
from sandbox.contract import SandboxPolicy
from sandbox.egressproxy.credentials import sandbox_proxy_env

from .conftest import COMPANY_NS, PLATFORM_NS, audit_story, sandbox_image

PROXY_URL = f"http://egress-proxy.{PLATFORM_NS}.svc.cluster.local:3128"

GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.com",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.com",
    "HOME": "/nonexistent",
}


def _kubectl(*args: str) -> str:
    proc = subprocess.run(
        ["kubectl", *args], capture_output=True, text=True, timeout=60
    )
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


@pytest.fixture(scope="module")
def egress_key(stack: None) -> str:
    out = _kubectl(
        "-n", PLATFORM_NS, "get", "secret", "egress-proxy-key",
        "-o", "go-template={{index .data \"EGRESS_PROXY_KEY\" | base64decode}}",
    )
    assert out
    return out


@pytest.fixture
def workspace_tar(tmp_path: Path) -> bytes:
    subprocess.run(["git", "init", "-b", "main"], cwd=tmp_path, env=GIT_ENV,
                   check=True, capture_output=True)
    (tmp_path / "hello.txt").write_text("hello\n")
    subprocess.run(["git", "add", "-A"], cwd=tmp_path, env=GIT_ENV,
                   check=True, capture_output=True)
    subprocess.run(["git", "commit", "-m", "base"], cwd=tmp_path, env=GIT_ENV,
                   check=True, capture_output=True)
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w") as tar:
        for entry in tmp_path.iterdir():
            tar.add(entry, arcname=entry.name)
    return buf.getvalue()


def test_egress_containment(egress_key: str, workspace_tar: bytes):
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    policy = SandboxPolicy(
        image=sandbox_image(),
        cpu="500m",
        memory="512Mi",
        env=sandbox_proxy_env(PROXY_URL, egress_key, run_id),
    )
    handle = backend.provision(run_id, workspace_tar, policy)
    try:
        # Non-allowlisted host: blocked at the proxy (403 on the tunnel).
        exit_code, output = backend._exec(
            handle.pod_name,
            ["python", "-c",
             "import urllib.request; urllib.request.urlopen('https://example.com', timeout=15)"],
        )
        assert exit_code != 0
        assert "403" in output

        # ... and the deny is in the audit sink, correlated by run_id.
        deadline = time.monotonic() + 30
        denies = []
        while time.monotonic() < deadline:
            denies = [e for e in audit_story(run_id) if e["decision"] == "deny"]
            if denies:
                break
            time.sleep(1)
        assert denies, "no correlated deny event in the registry audit store"
        assert denies[0]["rule"] == "egress_not_allowlisted"
        assert denies[0]["role"] == "sandbox-infra"

        # Allowlisted path: pip install through the proxy succeeds (the
        # proxy env vars are baked into the container environment).
        exit_code, output = backend._exec(
            handle.pod_name,
            ["pip", "install", "--no-cache-dir", "--quiet", "six"],
        )
        assert exit_code == 0, output
        allows = [e for e in audit_story(run_id) if e["decision"] == "allow"]
        assert allows, "pip install left no allow events"

        # NetworkPolicy backstop: bypassing the proxy is silently dropped.
        exit_code, _ = backend._exec(
            handle.pod_name,
            ["python", "-c",
             "import socket; s=socket.socket(); s.settimeout(5); s.connect(('1.1.1.1', 443))"],
        )
        assert exit_code != 0
    finally:
        backend.destroy(handle)
