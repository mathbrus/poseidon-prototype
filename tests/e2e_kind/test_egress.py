"""Egress enforcement against the live kind cluster: from inside a
sandbox, a request to a non-allowlisted host is blocked *and* the deny lands
in the registry audit store correlated by run_id; an allowlisted `pip install`
succeeds through the proxy; and the NetworkPolicy backstop silently drops
attempts to bypass the proxy.

The sandbox is provisioned exactly as the orchestrator does it: the per-run
proxy credential is minted with the shared key (read from the cluster
secret) and injected via `SandboxPolicy.env`, and the sandbox is registered
at the gateway — the proxy's allowlist resolves per run through that
registration to the applied blueprint's IR projection, so an unregistered
run has zero egress.
"""

import time
from pathlib import Path

import pytest

from common.ids import new_run_id
from sandbox.contract import SandboxPolicy
from sandbox.egressproxy.credentials import sandbox_proxy_env
from sandbox.k8s_jobs import K8sJobsBackend

from .cluster import (
    COMPANY_NS,
    admin_sandbox,
    audit_story,
    await_egress_deny,
    egress_proxy_key,
    egress_proxy_url,
    sandbox_image,
    workspace_tar,
)


@pytest.fixture(scope="module")
def proxy_url(stack: None) -> str:
    return egress_proxy_url()


@pytest.fixture(scope="module")
def egress_key(stack: None) -> str:
    return egress_proxy_key()


@pytest.fixture
def workspace(tmp_path: Path) -> bytes:
    return workspace_tar(tmp_path)


def test_egress_containment(env: dict, egress_key: str, proxy_url: str, workspace: bytes):
    backend = K8sJobsBackend(namespace=COMPANY_NS)
    run_id = new_run_id()
    policy = SandboxPolicy(
        image=sandbox_image(),
        cpu="500m",
        memory="512Mi",
        env=sandbox_proxy_env(proxy_url, egress_key, run_id),
    )
    handle = backend.provision(run_id, workspace, policy)
    admin_sandbox(env, "register", run_id, handle)
    try:
        # Non-allowlisted host: blocked at the proxy (403 on the tunnel).
        exit_code, output = backend._exec(
            handle.pod_name,
            [
                "python",
                "-c",
                "import urllib.request; urllib.request.urlopen('https://example.com', timeout=15)",
            ],
        )
        assert exit_code != 0
        assert "403" in output

        # ... and the deny is in the audit sink, correlated by run_id.
        assert await_egress_deny(run_id), (
            "no correlated deny event in the registry audit store"
        )

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
            [
                "python",
                "-c",
                "import socket; s=socket.socket(); s.settimeout(5); s.connect(('1.1.1.1', 443))",
            ],
        )
        assert exit_code != 0

        # Retiring the registration revokes egress entirely: the proxy
        # resolves the run to nothing (within its short cache TTL).
        admin_sandbox(env, "unregister", run_id)
        deadline = time.monotonic() + 30
        while True:
            exit_code, output = backend._exec(
                handle.pod_name,
                [
                    "python",
                    "-c",
                    "import urllib.request; urllib.request.urlopen("
                    "'https://pypi.org', timeout=15)",
                ],
            )
            if exit_code != 0 and "403" in output:
                break
            assert time.monotonic() < deadline, "egress survived unregistration"
            time.sleep(2)
    finally:
        backend.destroy(handle)
