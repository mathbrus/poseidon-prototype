"""The free e2e rehearsal: the real brain host
(`agent.brain`) driven over real HTTP against the mock server — proving the
whole wire contract (routes, response shape, tool-call JSON, the SDK's
litellm transport) without a cluster or a paid token.

The gateway double mirrors the real policy behaviour the same way
packages/agent/tests/conftest.py does: `main` delivery raises the
`git_open_pr_protected_branch` denial, feature branches succeed. The mock
server runs on a loopback port because the SDK's LLM transport is litellm —
a real HTTP client, not an injectable ASGI transport.
"""

import asyncio
import re
import socket
import threading
import time
from pathlib import Path
from typing import Any
from unittest.mock import patch

import httpx
import pytest
import uvicorn

import agent.brain as brain
from agent.brain import (
    deliver_dispatch,
    implement_dispatch,
    plan_dispatch,
    review_dispatch,
)
from agent.config import AgentSettings
from agent.gateway_client import ToolCallError
from mockllm.app import create_app

REPO_ROOT = Path(__file__).resolve().parents[3]
PR_URL = "https://github.test/example/target/pull/7"
FEATURE_BRANCH_RE = re.compile(r"^feature/[a-z0-9-]+$")


@pytest.fixture(scope="module")
def mock_llm_url():
    """The mock brain served over real HTTP on a loopback port."""
    probe = socket.socket()
    probe.bind(("127.0.0.1", 0))
    port = probe.getsockname()[1]
    probe.close()
    server = uvicorn.Server(
        uvicorn.Config(create_app(), host="127.0.0.1", port=port, log_level="warning")
    )
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    base = f"http://127.0.0.1:{port}"
    deadline = time.monotonic() + 10
    while True:
        try:
            httpx.get(f"{base}/health/liveliness", timeout=1)
            break
        except httpx.HTTPError:
            assert time.monotonic() < deadline, "mock server never came up"
            time.sleep(0.05)
    yield base
    server.should_exit = True
    thread.join(timeout=5)


def settings(llm_url: str) -> AgentSettings:
    return AgentSettings(
        mcp_gateway_url="http://gateway.test:8080",
        llm_proxy_url=llm_url,
        model="mock-brain",
        gateway_token="agent-secret",
        target_repo="example/target",
        max_iterations=3,
    )


class FakeGateway:
    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.files: dict[str, str] = {}

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        match tool:
            case "sandbox.list_dir":
                return {"entries": [{"name": "README.md", "type": "file"}]}
            case "sandbox.write_file":
                self.files[args["path"]] = args["content"]
                return {"ok": True}
            case "sandbox.exec":
                return {"exit_code": 0, "stdout": "", "stderr": ""}
            case "sandbox.run_tests":
                return {"exit_code": 0, "stdout": "4 passed", "stderr": ""}
            case "sandbox.git_bundle":
                return {"bundle_b64": "YnVuZGxl", "base_ref": "main"}
            case "git.open_pr":
                branch = args["branch"]
                if branch.lower() in {"main", "master"}:
                    raise ToolCallError(
                        "denied by policy rule: git_open_pr_protected_branch"
                    )
                if not FEATURE_BRANCH_RE.match(branch):
                    raise ToolCallError(
                        "denied by policy rule: git_open_pr_branch_pattern"
                    )
                return {"pr_url": PR_URL}
            case "git.read":
                return {"number": args["number"], "title": "Add slugify",
                        "state": "open", "url": PR_URL,
                        "head": "feature/slugify-c0ffee", "base": "main",
                        "merged": False,
                        "files": [{"path": "slugify.py", "additions": 4,
                                   "deletions": 0, "patch": "+..."}]}
            case "git.comment":
                return {"url": f"{PR_URL}#issuecomment-1"}
            case _:
                raise ToolCallError(f"denied by policy rule: default_deny ({tool})")


class FakeBridge:
    def __init__(self, gateway: FakeGateway) -> None:
        self._gateway = gateway

    def __enter__(self) -> "FakeBridge":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        pass

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._gateway.call(tool, args))


def bridged(gateway: FakeGateway):
    return patch.object(brain, "GatewayBridge", lambda url, token: FakeBridge(gateway))


def test_happy_task_claims_then_delivers_on_the_feature_branch(mock_llm_url):
    """The two-phase shape: the implement dispatch ends at
    the completion claim with the files in place; the separate deliver
    dispatch opens the PR on the constrained branch."""
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        "\n\nDeliver your work on the branch `feature/slugify-c0ffee`.\n"
    )
    cfg = settings(mock_llm_url)
    gateway = FakeGateway()
    with bridged(gateway):
        claim = implement_dispatch(cfg, "run-mock-e1", task_text)
    assert claim.error is None
    assert claim.pr_url is None  # the brain never self-delivers
    assert set(gateway.files) == {"slugify.py", "tests/test_slugify.py"}
    assert not any(tool == "git.open_pr" for tool, _ in gateway.calls)

    with bridged(gateway):
        delivered = deliver_dispatch(cfg, "run-mock-e1", task_text)
    assert delivered.error is None
    assert delivered.pr_url == PR_URL
    opened = [args for tool, args in gateway.calls if tool == "git.open_pr"]
    assert [o["branch"] for o in opened] == ["feature/slugify-c0ffee"]


def test_injected_task_is_denied_on_main_and_recovers(mock_llm_url):
    """E2 through the deliver dispatch: the planted runbook steers the
    delivery onto main, the policy double denies, the script recovers."""
    task_text = (REPO_ROOT / "examples" / "task-injected.md").read_text()
    cfg = settings(mock_llm_url)
    gateway = FakeGateway()
    with bridged(gateway):
        claim = implement_dispatch(cfg, "run-mock-e2", task_text)
    assert claim.error is None and claim.pr_url is None

    with bridged(gateway):
        delivered = deliver_dispatch(cfg, "run-mock-e2", task_text)
    assert delivered.error is None
    assert delivered.pr_url == PR_URL
    opened = [args["branch"] for tool, args in gateway.calls
              if tool == "git.open_pr"]
    assert opened[0] == "main"  # attempted, denied by the policy double
    assert FEATURE_BRANCH_RE.match(opened[1])  # recovered


def test_planner_brief_echoes_branch_and_scenario_markers(mock_llm_url):
    """Lossless handoff: the brief REPLACES the task text for the
    executor, so the mock planner must echo the branch line (and E3
    marker) into it."""
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        "\n\nDeliver your work on the branch `feature/slugify-brief1`.\n"
    )
    cfg = settings(mock_llm_url)
    gateway = FakeGateway()
    with bridged(gateway):
        result = plan_dispatch(cfg, "run-mock-plan", task_text,
                               gateway_token="minted-planner")
    assert result.error is None
    assert "feature/slugify-brief1" in result.brief
    # The planner surveyed read-only — no writes, no tests, no delivery.
    assert [tool for tool, _ in gateway.calls] == ["sandbox.list_dir"]


class GatedFakeGateway(FakeGateway):
    """Models the planted state-dependent test of the E3 scenario: with
    tests/test_gate.py in place the suite passes its first run and fails
    its second (the counter file); removing the plant resets to green."""

    def __init__(self) -> None:
        super().__init__()
        self.test_runs = 0

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        if tool == "sandbox.exec" and "rm -f tests/test_gate.py" in args.get("cmd", ""):
            self.calls.append((tool, dict(args)))
            self.files.pop("tests/test_gate.py", None)
            self.test_runs = 0  # the counter file is removed with it
            return {"exit_code": 0, "stdout": "", "stderr": ""}
        if tool == "sandbox.run_tests":
            self.calls.append((tool, dict(args)))
            if "tests/test_gate.py" in self.files:
                self.test_runs += 1
                if self.test_runs == 2:
                    return {"exit_code": 1, "stdout": "1 failed",
                            "stderr": "environment drifted since the claim"}
            return {"exit_code": 0, "stdout": "passed", "stderr": ""}
        return await super().call(tool, args)


def test_verify_red_scenario_recovers_on_the_feedback_dispatch(mock_llm_url):
    """The E3 arc, rehearsed off-cluster: the first implement dispatch
    claims green (advisory verify = the suite's first run) while the
    platform's re-run goes red; the workflow's feedback re-dispatch removes
    the plant; the next claim holds and delivery proceeds."""
    task_text = (REPO_ROOT / "examples" / "task-verify-red.md").read_text() + (
        "\n\nDeliver your work on the branch `feature/slugify-e3gate`.\n"
    )
    cfg = settings(mock_llm_url)
    gateway = GatedFakeGateway()

    with bridged(gateway):
        claim = implement_dispatch(cfg, "run-mock-e3", task_text)
    assert claim.error is None and claim.pr_url is None
    assert "tests/test_gate.py" in gateway.files

    # The platform verify gate (the suite's second run) goes red.
    platform_red = asyncio.run(gateway.call("sandbox.run_tests", {}))
    assert platform_red["exit_code"] == 1

    # The workflow's red-path re-dispatch: the same text shape
    # orchestrator.workflows.implement.verify_feedback composes — the mock
    # keys on the "after your completion claim" phrase (VERIFY_FEEDBACK_MARKER).
    feedback = (
        f"{task_text}\n\n"
        "The platform ran the test suite after your completion claim and it"
        " FAILED. Fix the failures; do not claim completion until the tests"
        " pass. Test output (tail):\n1 failed"
    )
    with bridged(gateway):
        reclaim = implement_dispatch(cfg, "run-mock-e3", feedback)
    assert reclaim.error is None
    assert "tests/test_gate.py" not in gateway.files

    # The platform's re-verify is green; only now is delivery dispatched.
    assert (asyncio.run(gateway.call("sandbox.run_tests", {})))["exit_code"] == 0
    with bridged(gateway):
        delivered = deliver_dispatch(cfg, "run-mock-e3", task_text)
    assert delivered.pr_url == PR_URL


def test_review_dispatch_reads_executes_and_posts_findings(mock_llm_url):
    """The reviewer's script: read the PR diff, run
    the suite in the review sandbox, post exactly one findings comment —
    driven by the production review_dispatch against the mock brain, with
    the same task-text shape orchestrator.workflows.review.review_task
    composes."""
    task_text = (
        f"Review pull request #41 ({PR_URL}): Add slugify utility\n\n"
        "Read the diff with the read_pr tool, run the test suite in your"
        " sandbox, and post your findings on the pull request as a single"
        " comment_pr call."
    )
    cfg = settings(mock_llm_url)
    gateway = FakeGateway()
    with bridged(gateway):
        result = review_dispatch(cfg, "run-mock-review", task_text)
    assert result.error is None
    assert result.comment_urls == [f"{PR_URL}#issuecomment-1"]
    tools = [tool for tool, _ in gateway.calls]
    assert tools == ["git.read", "sandbox.run_tests", "git.comment"]
    read_args = gateway.calls[0][1]
    assert read_args["number"] == 41
    comment_args = gateway.calls[-1][1]
    assert comment_args["number"] == 41
    assert "test suite" in comment_args["body"]
