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
    llm = OpenAICompatibleLLM("http://mockllm.test", "mock-brain")
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
        agent_bearer_token="agent-secret",
        target_repo="example/target",
        max_iterations=3,
    )


def test_happy_task_ends_in_a_feature_branch_pr():
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        "\n\nDeliver your work on the branch `feature/slugify-c0ffee`.\n"
    )
    gateway = FakeGateway()
    result = asyncio.run(
        run_graph(mock_llm(), gateway, settings(), "run-mock-e1", task_text)
    )
    assert result.error is None
    assert result.pr_url == PR_URL
    assert set(gateway.files) == {"slugify.py", "tests/test_slugify.py"}
    opened = [args for tool, args in gateway.calls if tool == "git.open_pr"]
    assert [o["branch"] for o in opened] == ["feature/slugify-c0ffee"]


def test_injected_task_is_denied_on_main_and_recovers():
    task_text = (REPO_ROOT / "examples" / "task-injected.md").read_text()
    gateway = FakeGateway()
    result = asyncio.run(
        run_graph(mock_llm(), gateway, settings(), "run-mock-e2", task_text)
    )
    assert result.error is None
    assert result.pr_url == PR_URL
    opened = [args["branch"] for tool, args in gateway.calls if tool == "git.open_pr"]
    assert opened[0] == "main"  # attempted, denied by the policy double
    assert FEATURE_BRANCH_RE.match(opened[1])  # recovered
