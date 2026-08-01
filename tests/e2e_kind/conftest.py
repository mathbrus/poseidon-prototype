"""Fixtures for the E1-on-kind suite (plan step 1d).

These tests run against the kind cluster brought up by
`deploy/kind/kind-up.sh`, with `deploy/kind/.env` filled with a real
GIT_TOKEN, LLM_API_KEY and a disposable TARGET_REPO. They open real pull
requests on that repo. The runner stays host-side (as on Compose) and
reaches the gateway/agent through the kind port mappings; sandboxes are
host-side Docker containers on the bridged sandbox network.

Run explicitly (excluded from plain `pytest`):

    uv run pytest tests/e2e_kind -m e2e_kind
"""

import asyncio
import json
import re
import secrets
import subprocess
from pathlib import Path
from typing import Any, Callable

import httpx
import pytest

from runner.cli import _run
from runner.config import RunnerSettings
from runner.flow import Manifest

REPO_ROOT = Path(__file__).resolve().parents[2]
ENV_FILE = REPO_ROOT / "deploy" / "kind" / ".env"
PLATFORM_NS = "poseidon-platform"
AUDIT_PATH = "/opt/poseidon/runs/audit.jsonl"

E2E_KIND_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook sees the whole session's items; only mark this suite's.
    for item in items:
        if item.path.is_relative_to(E2E_KIND_DIR):
            item.add_marker(pytest.mark.e2e_kind)


def cluster_audit_events(run_id: str) -> list[dict[str, Any]]:
    """All audit events for one run, read from the gateway's PVC-backed
    JSONL (the runner's host-side copy does not exist on kind)."""
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


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        pytest.fail(f"the E1 kind suite needs a filled {ENV_FILE} (copy .env.example)")
    values = {}
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, _, value = line.partition("=")
            values[key.strip()] = value.strip()
    return values


@pytest.fixture(scope="session")
def settings(env: dict[str, str]) -> RunnerSettings:
    return RunnerSettings(
        runner_bearer_token=env["RUNNER_BEARER_TOKEN"],
        mcp_gateway_url=env["MCP_GATEWAY_URL"],
        target_repo=env["TARGET_REPO"],
        base_ref=env.get("BASE_REF", "main"),
        agent_url=env["AGENT_URL"],
        sandbox_image=env.get("SANDBOX_IMAGE", "poseidon-sandbox"),
        sandbox_network=env.get("SANDBOX_NETWORK", "poseidon-kind-sandboxnet"),
        runs_dir=REPO_ROOT / "runs",
    )


@pytest.fixture(scope="session")
def stack(settings: RunnerSettings) -> None:
    """Fail fast (with a useful message) when the kind stack is not up."""
    hint = "is the cluster up? deploy/kind/kind-up.sh"
    try:
        resp = httpx.get(settings.mcp_gateway_url + "/mcp", timeout=5)
        if resp.status_code != 401:
            pytest.fail(f"gateway at {settings.mcp_gateway_url} answered "
                        f"{resp.status_code}, expected 401 without a token")
        resp = httpx.get(settings.agent_url + "/openapi.json", timeout=5)
        if resp.status_code != 200:
            pytest.fail(f"agent at {settings.agent_url} answered {resp.status_code}")
    except httpx.TransportError as exc:
        pytest.fail(f"cannot reach the stack ({exc}); {hint}")


@pytest.fixture(scope="session")
def happy_run(stack: None, settings: RunnerSettings) -> Manifest:
    """One full E1 run of examples/task-happy.md against the kind cluster.

    A nonce pins the delivery branch so reruns against the same disposable
    repo never collide on an existing branch or open PR.
    """
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    return asyncio.run(_run(settings, task_text))


@pytest.fixture(scope="session")
def pr_info(env: dict[str, str]) -> Callable[[str], dict[str, Any]]:
    """Fetch a pull request from the GitHub API given its html URL."""

    def fetch(pr_url: str) -> dict[str, Any]:
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
