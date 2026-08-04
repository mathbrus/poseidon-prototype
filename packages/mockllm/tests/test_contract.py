"""The free e2e rehearsal: the real agent graph (`agent.graph.run_graph`)
driven by the real OpenAI client wiring against the mock server over an
in-process ASGI transport — proving the whole wire contract (routes,
response shape, tool-call JSON) without a cluster or a paid token.

The gateway double mirrors the real policy behaviour the same way
packages/agent/tests/conftest.py does: `main` delivery raises the
`git_open_pr_protected_branch` denial, feature branches succeed.
"""

import asyncio
import re
from pathlib import Path
from typing import Any

import httpx
from openai import AsyncOpenAI

from agent.config import AgentSettings
from agent.gateway_client import ToolCallError
from agent.graph import run_graph
from agent.llm import OpenAICompatibleLLM
from mockllm.app import create_app


def deliver_through_mock(gateway, task_text: str, run_id: str):
    """Drive the workflow-directed deliver dispatch (plan step 19) with the
    production run_deliver against the mock brain and a fake gateway."""
    import agent.graph as graph_module
    from agent.graph import run_deliver

    llm = mock_llm()

    class FakeGatewayCtx:
        async def __aenter__(self):
            return gateway
        async def __aexit__(self, *exc):
            return False

    original_llm = graph_module.OpenAICompatibleLLM
    original_gw = graph_module.McpGateway
    graph_module.OpenAICompatibleLLM = lambda *a, **k: llm
    graph_module.McpGateway = lambda *a, **k: FakeGatewayCtx()

    async def go():
        try:
            return await run_deliver(settings(), run_id, task_text)
        finally:
            graph_module.OpenAICompatibleLLM = original_llm
            graph_module.McpGateway = original_gw

    return asyncio.run(go())

REPO_ROOT = Path(__file__).resolve().parents[3]
PR_URL = "https://github.test/example/target/pull/7"
FEATURE_BRANCH_RE = re.compile(r"^feature/[a-z0-9-]+$")


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
            case _:
                raise ToolCallError(f"denied by policy rule: default_deny ({tool})")


def mock_llm() -> OpenAICompatibleLLM:
    """The production LLM client, transported in-process into the mock app."""
    llm = OpenAICompatibleLLM(
        "http://mockllm.test", "mock-brain",
        token="test-token", run_id="run-mock",
    )
    llm._client = AsyncOpenAI(
        base_url="http://mockllm.test",
        api_key="poseidon-agent",
        http_client=httpx.AsyncClient(transport=httpx.ASGITransport(app=create_app())),
    )
    return llm


def settings() -> AgentSettings:
    return AgentSettings(
        mcp_gateway_url="http://gateway.test:8080",
        llm_proxy_url="http://mockllm.test",
        model="mock-brain",
        gateway_token="agent-secret",
        target_repo="example/target",
        max_iterations=3,
    )


def test_happy_task_claims_then_delivers_on_the_feature_branch():
    """The two-phase shape (plan step 19): the implement dispatch ends at
    the completion claim with the files in place; the separate deliver
    dispatch opens the PR on the constrained branch."""
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        "\n\nDeliver your work on the branch `feature/slugify-c0ffee`.\n"
    )
    gateway = FakeGateway()
    claim = asyncio.run(
        run_graph(mock_llm(), gateway, settings(), "run-mock-e1", task_text)
    )
    assert claim.error is None
    assert claim.pr_url is None  # the brain never self-delivers
    assert set(gateway.files) == {"slugify.py", "tests/test_slugify.py"}
    assert not any(tool == "git.open_pr" for tool, _ in gateway.calls)

    delivered = deliver_through_mock(gateway, task_text, "run-mock-e1")
    assert delivered.error is None
    assert delivered.pr_url == PR_URL
    opened = [args for tool, args in gateway.calls if tool == "git.open_pr"]
    assert [o["branch"] for o in opened] == ["feature/slugify-c0ffee"]


def test_injected_task_is_denied_on_main_and_recovers():
    """E2 through the deliver dispatch: the planted runbook steers the
    delivery onto main, the policy double denies, the script recovers."""
    task_text = (REPO_ROOT / "examples" / "task-injected.md").read_text()
    gateway = FakeGateway()
    claim = asyncio.run(
        run_graph(mock_llm(), gateway, settings(), "run-mock-e2", task_text)
    )
    assert claim.error is None and claim.pr_url is None

    delivered = deliver_through_mock(gateway, task_text, "run-mock-e2")
    assert delivered.error is None
    assert delivered.pr_url == PR_URL
    opened = [args["branch"] for tool, args in gateway.calls if tool == "git.open_pr"]
    assert opened[0] == "main"  # attempted, denied by the policy double
    assert FEATURE_BRANCH_RE.match(opened[1])  # recovered


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


def test_verify_red_scenario_recovers_on_the_feedback_dispatch():
    """The E3 arc, rehearsed off-cluster: the first implement dispatch
    claims green (advisory verify = the suite's first run) while the
    platform's re-run goes red; the workflow's feedback re-dispatch removes
    the plant; the next claim holds and delivery proceeds."""
    task_text = (REPO_ROOT / "examples" / "task-verify-red.md").read_text() + (
        "\n\nDeliver your work on the branch `feature/slugify-e3gate`.\n"
    )
    gateway = GatedFakeGateway()

    claim = asyncio.run(
        run_graph(mock_llm(), gateway, settings(), "run-mock-e3", task_text)
    )
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
    reclaim = asyncio.run(
        run_graph(mock_llm(), gateway, settings(), "run-mock-e3", feedback)
    )
    assert reclaim.error is None
    assert "tests/test_gate.py" not in gateway.files

    # The platform's re-verify is green; only now is delivery dispatched.
    assert (asyncio.run(gateway.call("sandbox.run_tests", {})))["exit_code"] == 0
    delivered = deliver_through_mock(gateway, task_text, "run-mock-e3")
    assert delivered.pr_url == PR_URL
