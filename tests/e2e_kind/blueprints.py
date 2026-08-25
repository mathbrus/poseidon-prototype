"""Applying the example blueprints to the cluster, exactly as an operator
of the platform does, and waiting for the company to converge — which
includes policy: OPA must have activated the company's bundle, not just
the pods be Ready."""

import asyncio
import contextlib
import subprocess
import time
from pathlib import Path

import httpx
from temporalio.client import Client

from orchestrator.workflows.base import workflow_id

from .cluster import COMPANY_NS, kubectl, port_forward, temporal_port_forward

REPO_ROOT = Path(__file__).resolve().parents[2]
EXAMPLES = REPO_ROOT / "examples"
HELLO_SAAS = EXAMPLES / "hello-saas.yaml"
CONDUCTED = EXAMPLES / "hello-saas-conducted.yaml"
DELEGATED = EXAMPLES / "hello-saas-delegated.yaml"


def apply_blueprint(path: Path) -> None:
    """`poseidon blueprint apply`. The registry may be unreachable from the
    host — apply degrades to diffing against nothing and still submits
    the CRs."""
    proc = subprocess.run(
        ["uv", "run", "poseidon", "blueprint", "apply", str(path), "--yes"],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert proc.returncode == 0, f"blueprint apply failed: {proc.stderr}"


def wait_for_convergence(timeout_s: int = 300) -> None:
    """The company namespace is Active, its one standing hire (the PM) is
    rolled out, and OPA serves the company's decision document. The last
    part matters after a teardown + re-apply: OPA needs a poll cycle
    (5-15s) to activate the bundle, and a test that runs before it would
    land in a `no_policy_installed` window."""
    kubectl(
        "wait",
        "--for=jsonpath={.status.phase}=Active",
        f"ns/{COMPANY_NS}",
        f"--timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
    kubectl(
        "-n",
        COMPANY_NS,
        "rollout",
        "status",
        "deployment/pm",
        f"--timeout={timeout_s}s",
        timeout=timeout_s + 30,
    )
    with port_forward("svc/opa", 8181) as port:
        deadline = time.monotonic() + 120
        while True:
            decision = httpx.get(
                f"http://127.0.0.1:{port}/v1/data/poseidon/authz/hello_saas/decision",
                timeout=10,
            ).json()
            if "result" in decision:
                return
            assert time.monotonic() < deadline, (
                "OPA never activated the re-applied company bundle"
            )
            time.sleep(3)


def bundle_revision_from_cr() -> str:
    return kubectl(
        "get", "company", COMPANY_NS, "-o", "jsonpath={.status.bundleRevision}"
    )


def quiesce_heartbeat() -> None:
    """Terminate the ensure loop's bare `conduct-{company}` heartbeat after
    the pristine blueprint is back. Revocation-by-diff stops it on its own
    — the product claim, pinned at the workflow layer — but on the
    blueprint's `heartbeat_minutes` cadence, which would leave a competitor
    asleep through the next module's arc. The campsite rule wants the
    quiet now: call this after the pristine apply, so the ensure loop has
    nothing to restart."""

    async def terminate() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(workflow_id("conduct", COMPANY_NS))
            with contextlib.suppress(Exception):
                await handle.terminate("suite teardown: pristine blueprint restored")

    asyncio.run(terminate())


def restore_pristine() -> None:
    """Back to `hello-saas.yaml`, converged and quiet — what every module
    that applied another profile owes the modules after it."""
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()
    quiesce_heartbeat()
