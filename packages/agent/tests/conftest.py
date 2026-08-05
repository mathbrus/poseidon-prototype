"""Brain-host unit-test doubles (plan step 22): a scripted litellm patch
driving the real OpenHands SDK loop, and a fake gateway (behind a sync fake
bridge) that mimics the real policy behaviour — feature-branch rule
included — closely enough to exercise allow, deny and recovery paths."""

import asyncio
import json
import re
from contextlib import contextmanager
from itertools import count
from typing import Any
from unittest.mock import patch

import pytest
from litellm.types.utils import ModelResponse

import agent.brain as brain
from agent.config import AgentSettings
from agent.gateway_client import ToolCallError

RUN_ID = "run-01TESTULID"
PR_URL = "https://github.test/example/target/pull/7"
COMMENT_URL = "https://github.test/example/target/pull/7#issuecomment-1"
FEATURE_BRANCH_RE = re.compile(r"^feature/[a-z0-9-]+$")

_ids = count(1)


def says(content: str) -> ModelResponse:
    """A plain-text completion — what ends an SDK conversation."""
    return ModelResponse(
        choices=[{
            "index": 0,
            "finish_reason": "stop",
            "message": {"role": "assistant", "content": content},
        }]
    )


def calls(*tool_calls: tuple[str, dict[str, Any]]) -> ModelResponse:
    return ModelResponse(
        choices=[{
            "index": 0,
            "finish_reason": "tool_calls",
            "message": {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": f"call-{next(_ids)}",
                        "type": "function",
                        "function": {"name": name, "arguments": json.dumps(args)},
                    }
                    for name, args in tool_calls
                ],
            },
        }]
    )


def tc(name: str, **arguments: Any) -> tuple[str, dict[str, Any]]:
    return (name, arguments)


def text_of(message: dict[str, Any]) -> str:
    """Message text from either OpenAI string content or SDK content blocks."""
    content = message.get("content")
    if isinstance(content, list):
        return "".join(
            str(b.get("text", "")) for b in content if isinstance(b, dict)
        )
    return str(content or "")


class ScriptedLLM:
    """Pops pre-scripted litellm responses in order; records every request
    the SDK makes (messages + offered tools)."""

    def __init__(self, script: list[ModelResponse]) -> None:
        self.script = list(script)
        self.requests: list[tuple[list[dict], list[dict] | None]] = []

    def __call__(self, *args: Any, **kwargs: Any) -> ModelResponse:
        self.requests.append((kwargs.get("messages"), kwargs.get("tools")))
        assert self.script, "LLM script exhausted — loop made an unexpected LLM call"
        return self.script.pop(0)

    def offered(self, request_index: int) -> list[str]:
        _, tools = self.requests[request_index]
        return [t["function"]["name"] for t in tools or []]


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
            case "issues.create":
                return {"number": 7, "state": "open", "title": args["title"],
                        "body": args["body"], "url": "https://github.test/i/7"}
            case "issues.read":
                return {"issues": []}
            case "crew.request":
                return {"id": "crq-abc123", "status": "recorded",
                        "conductor": "pm-1",
                        "expires_at": "2026-01-01T01:00:00+00:00"}
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


class FakeBridge:
    """Sync facade over the async FakeGateway, shaped like GatewayBridge."""

    def __init__(self, gateway: FakeGateway) -> None:
        self._gateway = gateway

    def __enter__(self) -> "FakeBridge":
        return self

    def __exit__(self, *exc_info: Any) -> None:
        pass

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run(self._gateway.call(tool, args))


@contextmanager
def scripted(gateway: FakeGateway, llm: ScriptedLLM):
    """Run a brain dispatch against the doubles: the fake bridge replaces
    the MCP session, the scripted responses replace the LLM transport —
    everything in between is the production loop."""
    with patch.object(brain, "GatewayBridge", lambda url, token: FakeBridge(gateway)):
        with patch("openhands.sdk.llm.llm.litellm_completion", side_effect=llm):
            yield


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
