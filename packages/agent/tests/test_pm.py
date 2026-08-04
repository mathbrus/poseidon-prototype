"""The PM runtime (plan step 17) against doubles: scope -> ticket -> crew
intent -> summary, through governed tools only; a gateway deny surfaces as
a tool error the model sees, never a crash."""

import asyncio
from typing import Any

import pytest

from agent.config import AgentSettings
from agent.gateway_client import ToolCallError
from agent.graph import run_pm
from conftest import ScriptedLLM, calls, says, tc


@pytest.fixture
def settings(tmp_path) -> AgentSettings:
    token = tmp_path / "token"
    token.write_text("pm-projected")
    return AgentSettings(
        mcp_gateway_url="http://gateway.test:8080",
        llm_proxy_url="http://llm.test:4000",
        model="mock-brain",
        role_template="pm",
        gateway_token_file=str(token),
        target_repo="github.test/example/target",
    )


class PmGateway:
    """The PM-visible slice of the gateway: issues + crew intent; every
    executor tool is a policy deny."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, dict[str, Any]]] = []

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        self.calls.append((tool, dict(args)))
        match tool:
            case "issues.create":
                return {"number": 7, "state": "open", "title": args["title"],
                        "body": args["body"], "url": "https://github.test/i/7"}
            case "issues.read":
                return {"issues": []}
            case "crew.request":
                return {"id": "crq-abc123", "status": "recorded",
                        "conductor": "pm-1",
                        "expires_at": "2026-01-01T01:00:00+00:00"}
            case _:
                raise ToolCallError(f"denied by policy rule: default_deny ({tool})")


def run(settings, llm, gateway):
    import agent.graph as graph_module

    async def go():
        # Patch the LLM + gateway construction points.
        original_llm = graph_module.OpenAICompatibleLLM
        original_gw = graph_module.McpGateway

        class FakeGatewayCtx:
            def __init__(self, *a, **k): ...
            async def __aenter__(self):
                return gateway
            async def __aexit__(self, *exc):
                return False

        graph_module.OpenAICompatibleLLM = lambda *a, **k: llm
        graph_module.McpGateway = FakeGatewayCtx
        try:
            return await run_pm(settings, "run-pm-1", "Ship a slug endpoint")
        finally:
            graph_module.OpenAICompatibleLLM = original_llm
            graph_module.McpGateway = original_gw

    return asyncio.run(go())


def test_pm_files_ticket_and_records_intent(settings):
    llm = ScriptedLLM([
        calls(tc("read_issues")),
        calls(tc("create_issue", title="Implement slug endpoint",
                 body="acceptance: ...")),
        calls(tc("request_crew", crew_template="feature-delivery",
                 reason="ticket 7")),
        says("Set up: ticket 7, crew requested."),
    ])
    gateway = PmGateway()
    result = run(settings, llm, gateway)

    assert result.error is None
    assert result.ticket_urls == ["https://github.test/i/7"]
    assert result.crew_request_id == "crq-abc123"
    tools = [tool for tool, _ in gateway.calls]
    assert tools == ["issues.read", "issues.create", "crew.request"]


def test_pm_deny_surfaces_as_tool_error_not_crash(settings):
    """§4.5 exit is the gateway's: a PM told to exec sees the deny as an
    error message and can carry on scoping."""
    llm = ScriptedLLM([
        calls(tc("exec", cmd="rm -rf /")),  # not a PM tool at all
        calls(tc("create_issue", title="t", body="b")),
        says("done"),
    ])
    gateway = PmGateway()
    result = run(settings, llm, gateway)
    assert result.error is None
    assert result.ticket_urls == ["https://github.test/i/7"]
    # The forbidden call never reached the gateway (not in PM_TOOLS).
    assert all(tool != "sandbox.exec" for tool, _ in gateway.calls)


def test_pm_without_a_ticket_fails_honestly(settings):
    llm = ScriptedLLM([says("nothing to do here")])
    result = run(settings, llm, PmGateway())
    assert result.error == "PM filed no ticket for the goal"
