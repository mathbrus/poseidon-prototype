"""Fixtures for the kind e2e suite (plan steps 1d + 3).

These tests run against the kind cluster brought up by
`deploy/kind/kind-up.sh`, with `deploy/kind/.env` filled with a real
GIT_TOKEN and a disposable TARGET_REPO. They open real pull requests on
that repo. The LLM is the deterministic mock by default (free); only a
cluster deployed with REAL_LLM=1 spends provider tokens.

Since plan step 3, runs go through Temporal: the suite starts the implement
workflow as a thin client (exactly what `poseidon run` does), and the
in-cluster orchestrator worker drives the activities. Mid-run, the worker
pod is killed — the E1 assertions double as the step-3 crash-resume proof.

Run explicitly (excluded from plain `pytest`):

    uv run pytest tests/e2e_kind -m e2e_kind
"""

import asyncio
import contextlib
import json
import secrets
import socket
import subprocess
import time
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest
from temporalio.client import Client

from common.ids import new_run_id
from orchestrator.workflows.implement import ImplementInput, workflow_id

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "deploy" / "kind" / ".env"
PLATFORM_NS = "poseidon-platform"
COMPANY_NS = "hello-saas"
RUN_ID_LABEL = "poseidon.io/run-id"
AUDIT_PATH = "/opt/poseidon/runs/audit.jsonl"
TASK_QUEUE = "poseidon-orchestrator"
TEMPORAL_LOCAL_PORT = 17233

E2E_KIND_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook sees the whole session's items; only mark this suite's.
    for item in items:
        if item.path.is_relative_to(E2E_KIND_DIR):
            item.add_marker(pytest.mark.e2e_kind)


@contextlib.contextmanager
def temporal_port_forward():
    """Host access to the Temporal frontend, on demand (the kind
    extraPortMappings are fixed at cluster creation)."""
    proc = subprocess.Popen(
        [
            "kubectl", "-n", PLATFORM_NS,
            "port-forward", "svc/temporal-frontend",
            f"{TEMPORAL_LOCAL_PORT}:7233",
        ],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            if proc.poll() is not None:
                pytest.fail("kubectl port-forward exited early")
            try:
                with socket.create_connection(
                    ("127.0.0.1", TEMPORAL_LOCAL_PORT), timeout=1
                ):
                    break
            except OSError:
                if time.monotonic() > deadline:
                    pytest.fail("port-forward to temporal-frontend never came up")
                time.sleep(0.5)
        yield f"127.0.0.1:{TEMPORAL_LOCAL_PORT}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def cluster_audit_events(run_id: str) -> list[dict[str, Any]]:
    """All audit events for one run, read from the gateway's PVC-backed
    JSONL (nothing audit-related exists host-side anymore)."""
    proc = subprocess.run(
        [
            "kubectl", "-n", PLATFORM_NS,
            "exec", "deployment/mcp-gateway", "--",
            "cat", AUDIT_PATH,
        ],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, f"cannot read the audit log: {proc.stderr}"
    events = []
    for line in proc.stdout.splitlines():
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            continue
        if event.get("run_id") == run_id:
            events.append(event)
    return events


def sandbox_pods(run_id: str | None = None) -> list[str]:
    """Names of live (non-terminated) sandbox pods, optionally for one run.
    Sandboxes are Jobs in the company namespace since plan step 4."""
    selector = f"{RUN_ID_LABEL}={run_id}" if run_id else RUN_ID_LABEL
    proc = subprocess.run(
        [
            "kubectl", "-n", COMPANY_NS,
            "get", "pods", "-l", selector,
            "-o", "jsonpath={range .items[*]}{.metadata.name} {.status.phase}{'\\n'}{end}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert proc.returncode == 0, proc.stderr
    return [
        name
        for line in proc.stdout.splitlines()
        if line.strip()
        for name, _, phase in [line.partition(" ")]
        if phase.strip() not in ("Succeeded", "Failed")
    ]


def kill_worker_pod() -> None:
    subprocess.run(
        [
            "kubectl", "-n", PLATFORM_NS,
            "delete", "pod", "-l", "app=orchestrator-worker", "--wait=false",
        ],
        check=True, capture_output=True, timeout=60,
    )


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        pytest.fail(f"the kind e2e suite needs a filled {ENV_FILE} (copy .env.example)")
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def stack(env: dict[str, str]) -> None:
    """Fail fast (with a useful message) when the kind stack is not up."""
    hint = "is the cluster up? deploy/kind/kind-up.sh"
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    try:
        resp = httpx.get(gateway_url + "/mcp", timeout=5)
        if resp.status_code != 401:
            pytest.fail(f"gateway at {gateway_url} answered "
                        f"{resp.status_code}, expected 401 without a token")
    except httpx.TransportError as exc:
        pytest.fail(f"cannot reach the stack ({exc}); {hint}")
    ready = subprocess.run(
        [
            "kubectl", "-n", PLATFORM_NS,
            "get", "deployment/orchestrator-worker",
            "-o", "jsonpath={.status.readyReplicas}",
        ],
        capture_output=True, text=True, timeout=30,
    )
    if ready.returncode != 0 or ready.stdout.strip() != "1":
        pytest.fail(f"orchestrator worker not ready; {hint}")


async def _execute_implement(address: str, run_id: str, task_text: str) -> dict:
    """Start implement exactly as `poseidon run` does, kill the worker once
    the sandbox exists (the step-3 crash-resume proof), await the result."""
    client = await Client.connect(address)
    handle = await client.start_workflow(
        "implement",
        ImplementInput(run_id=run_id, task_text=task_text),
        id=workflow_id(run_id),
        task_queue=TASK_QUEUE,
    )
    deadline = time.monotonic() + 300
    while not sandbox_pods(run_id):
        if time.monotonic() > deadline:
            raise AssertionError("sandbox never appeared; cannot stage worker kill")
        await asyncio.sleep(2)
    kill_worker_pod()
    return await asyncio.wait_for(handle.result(), timeout=1800)


@pytest.fixture(scope="session")
def implement_run(stack: None) -> dict:
    """One full implement run of examples/task-happy.md through Temporal,
    with the worker killed mid-run.

    A nonce pins the delivery branch so reruns against the same disposable
    repo never collide on an existing branch or open PR.
    """
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    run_id = new_run_id()
    with temporal_port_forward() as address:
        record = asyncio.run(_execute_implement(address, run_id, task_text))
    assert record["run_id"] == run_id
    return record


@pytest.fixture(scope="session")
def pr_info(env: dict[str, str]) -> Callable[[str], dict[str, Any]]:
    """Fetch a pull request from the GitHub API given its html URL."""

    def fetch(pr_url: str) -> dict[str, Any]:
        import re

        match = re.fullmatch(r"https://github\.com/(.+?/.+?)/pull/(\d+)", pr_url)
        assert match, f"unexpected PR URL: {pr_url}"
        resp = httpx.get(
            f"https://api.github.com/repos/{match[1]}/pulls/{match[2]}",
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {env['GIT_TOKEN']}",
            },
            timeout=30,
        )
        assert resp.status_code == 200, f"GitHub API: {resp.status_code} {resp.text}"
        return resp.json()

    return fetch
