"""Runner unit-test doubles: every collaborator of the flow (gateway, agent,
sandbox backend) is an in-memory fake recording its interactions."""

import json
from pathlib import Path
from typing import Any

import pytest

from runner.config import RunnerSettings
from sandbox.contract import SandboxHandle

RUN_ID = "run-01TESTULID"
PR_URL = "https://github.test/example/target/pull/9"
WORKSPACE_TAR = b"tar-bytes-of-the-workspace"
SANDBOX_URL = "http://poseidon-sbx-run-01TESTULID:8000"


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def settings(tmp_path: Path) -> RunnerSettings:
    return RunnerSettings(
        runner_bearer_token="runner-secret",
        mcp_gateway_url="http://gateway.test:8080",
        target_repo="https://github.test/example/target",
        base_ref="main",
        agent_url="http://agent.test:8000",
        runs_dir=tmp_path / "runs",
        step_timeout_s=1.0,
        agent_timeout_s=1.0,
        poll_interval_s=0.01,
    )


class FakeBackend:
    def __init__(self) -> None:
        self.events: list[str] = []
        self.provision_error: Exception | None = None

    def provision(self, run_id: str, workspace_tar: bytes) -> SandboxHandle:
        self.events.append("provision")
        if self.provision_error:
            raise self.provision_error
        assert workspace_tar == WORKSPACE_TAR
        return SandboxHandle(run_id=run_id, container_id="cafe1234", url=SANDBOX_URL)

    def destroy(self, handle: SandboxHandle) -> None:
        self.events.append("destroy")


class FakeGateway:
    def __init__(self) -> None:
        self.events: list[tuple[str, Any]] = []
        self.fetch_error: Exception | None = None
        self.register_error: Exception | None = None

    async def fetch_workspace(self, repo: str, ref: str) -> bytes:
        self.events.append(("fetch_workspace", (repo, ref)))
        if self.fetch_error:
            raise self.fetch_error
        return WORKSPACE_TAR

    async def register_sandbox(self, run_id: str, url: str) -> None:
        self.events.append(("register_sandbox", (run_id, url)))
        if self.register_error:
            raise self.register_error


class FakeAgent:
    """Returns 'running' `running_polls` times, then the terminal status."""

    def __init__(
        self,
        terminal: dict[str, Any] | None = None,
        running_polls: int = 2,
        hang: bool = False,
    ) -> None:
        self.dispatched: list[tuple[str, str]] = []
        self.polls = 0
        self.running_polls = running_polls
        self.hang = hang
        self.terminal = terminal or {
            "status": "done", "pr_url": PR_URL, "iterations": 2, "error": None,
        }

    async def dispatch(self, run_id: str, task_text: str) -> None:
        self.dispatched.append((run_id, task_text))

    async def status(self, run_id: str) -> dict[str, Any]:
        self.polls += 1
        if self.hang or self.polls <= self.running_polls:
            return {"status": "running", "pr_url": None, "iterations": 0, "error": None}
        return self.terminal


def write_audit_lines(settings: RunnerSettings, lines: list[dict[str, Any]]) -> None:
    settings.audit_path.parent.mkdir(parents=True, exist_ok=True)
    with settings.audit_path.open("a") as f:
        for line in lines:
            f.write(json.dumps(line) + "\n")
