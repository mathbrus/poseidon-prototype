"""Shared fixtures for the E2E suite (S10, exit criteria E1–E4).

These tests run against the real system: the compose stack up and healthy
(`cd deploy/compose && docker compose up -d --build`), the sandbox image
built (`docker build -f Dockerfile.sandbox -t poseidon-sandbox .`), and
`deploy/compose/.env` filled with a real GIT_TOKEN, LLM_API_KEY and a
disposable TARGET_REPO. They open real pull requests on that repo.

Run them explicitly (they are excluded from plain `pytest`):

    uv run pytest tests/e2e -m e2e

The two agent runs (happy and injected task) are session-scoped fixtures, so
the whole suite costs exactly two runs regardless of how many tests use them.
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
COMPOSE_DIR = REPO_ROOT / "deploy" / "compose"
ENV_FILE = COMPOSE_DIR / ".env"


E2E_DIR = Path(__file__).resolve().parent


def pytest_collection_modifyitems(items: list[pytest.Item]) -> None:
    # The hook sees the whole session's items; only mark this suite's.
    for item in items:
        if item.path.is_relative_to(E2E_DIR):
            item.add_marker(pytest.mark.e2e)


def compose_exec(service: str, *cmd: str) -> subprocess.CompletedProcess:
    """Run a command inside a compose service container."""
    return subprocess.run(
        ["docker", "compose", "exec", "-T", service, *cmd],
        cwd=COMPOSE_DIR,
        capture_output=True,
        text=True,
        timeout=60,
    )


def audit_events(settings: RunnerSettings, run_id: str) -> list[dict[str, Any]]:
    """All audit events for one run, in file (= append) order."""
    events = []
    for line in settings.audit_path.read_text().splitlines():
        event = json.loads(line)
        if event.get("run_id") == run_id:
            events.append(event)
    return events


@pytest.fixture(scope="session")
def env() -> dict[str, str]:
    if not ENV_FILE.is_file():
        pytest.fail(f"E2E tests need a filled {ENV_FILE} (copy .env.example)")
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
        sandbox_network=env.get("SANDBOX_NETWORK", "poseidon-sandboxnet"),
        runs_dir=REPO_ROOT / "runs",
    )


@pytest.fixture(scope="session")
def stack(env: dict[str, str], settings: RunnerSettings) -> None:
    """Fail fast (with a useful message) when the compose stack is not up."""
    hint = "is the stack up? cd deploy/compose && docker compose up -d --build"
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


def _execute(settings: RunnerSettings, task_text: str) -> Manifest:
    return asyncio.run(_run(settings, task_text))


@pytest.fixture(scope="session")
def happy_run(stack: None, settings: RunnerSettings) -> Manifest:
    """One full E1 run of examples/task-happy.md.

    A nonce pins the delivery branch so reruns against the same disposable
    repo never collide on an existing branch or open PR.
    """
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    return _execute(settings, task_text)


@pytest.fixture(scope="session")
def injected_run(stack: None, settings: RunnerSettings) -> Manifest:
    """One full E2 run of examples/task-injected.md.

    No branch hint is added: the injected task tells the agent to deliver on
    `main`, and the point of E2 is that *policy*, not the prompt, stops that.
    The recovery branch the agent then picks is left to it; the nonce in the
    injected file's fallback instruction keeps reruns from colliding.
    """
    text = (REPO_ROOT / "examples" / "task-injected.md").read_text()
    task_text = text.replace("feature/<slug>", f"feature/slugify-{secrets.token_hex(3)}")
    return _execute(settings, task_text)


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
