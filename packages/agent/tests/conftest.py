"""Agent unit-test doubles: a scripted LLM and a fake MCP gateway that
mimics the real policy behaviour (feature-branch rule included) closely
enough to exercise the graph's allow, deny and retry paths."""

import re
from itertools import count
from typing import Any

import pytest

from agent.config import AgentSettings
from agent.gateway_client import ToolCallError
from agent.llm import Completion, ToolCall

RUN_ID = "run-01TESTULID"
PR_URL = "https://github.test/example/target/pull/7"
COMMENT_URL = "https://github.test/example/target/pull/7#issuecomment-1"
FEATURE_BRANCH_RE = re.compile(r"^feature/[a-z0-9-]+$")

_ids = count(1)


def tc(name: str, **arguments: Any) -> ToolCall:
    return ToolCall(id=f"call-{next(_ids)}", name=name, arguments=arguments)


def says(content: str) -> Completion:
    return Completion(content=content)


def calls(*tool_calls: ToolCall) -> Completion:
    return Completion(content=None, tool_calls=list(tool_calls))


class ScriptedLLM:
    """Pops pre-scripted completions in order; records every request."""

    def __init__(self, script: list[Completion]) -> None:
        self.script = list(script)
        self.requests: list[tuple[list[dict], list[dict] | None]] = []

    async def complete(
        self, messages: list[dict], tools: list[dict] | None = None
    ) -> Completion:
        self.requests.append(([dict(m) for m in messages], tools))
        assert self.script, "LLM script exhausted — graph made an unexpected LLM call"
        return self.script.pop(0)


TESTS_PASS = {"exit_code": 0, "stdout": "3 passed", "stderr": ""}
TESTS_FAIL = {"exit_code": 1, "stdout": "1 failed: test_hello", "stderr": ""}


class FakeGateway:
    """Stands in for the gateway MCP surface; policy denials raise
    ToolCallError with the rule name in the text, like the real gateway."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []
        self.default_test_result: dict[str, Any] = TESTS_PASS

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        match tool:
            case "sandbox.list_dir":
                return {
                    "entries": [
                        {"name": "README.md", "type": "file"},
                        {"name": "src", "type": "dir"},
                    ]
                }
            case "sandbox.read_file":
                return {"content": "def hello():\n    return 'hi'\n"}
            case "sandbox.write_file":
                return {"ok": True}
            case "sandbox.exec":
                return {"exit_code": 0, "stdout": "", "stderr": ""}
            case "sandbox.run_tests":
                return dict(self.default_test_result)
            case "sandbox.git_bundle":
                return {"bundle_b64": "YnVuZGxl", "base_ref": "main"}
            case "git.read":
                return {
                    "number": args["number"],
                    "title": "Add greet",
                    "state": "open",
                    "url": PR_URL,
                    "head": "feature/greet",
                    "base": "main",
                    "merged": False,
                    "files": [{"path": "src/hello.py", "additions": 2,
                               "deletions": 0, "patch": "+def greet..."}],
                }
            case "git.comment":
                return {"url": COMMENT_URL}
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

    def tools_called(self) -> list[str]:
        return [tool for tool, _ in self.calls]


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def gateway() -> FakeGateway:
    return FakeGateway()


@pytest.fixture
def settings() -> AgentSettings:
    return AgentSettings(
        mcp_gateway_url="http://gateway.test:8080",
        llm_proxy_url="http://llm.test:4000",
        model="test-model",
        gateway_token="agent-secret",
        target_repo="example/target",
        max_iterations=3,
    )
