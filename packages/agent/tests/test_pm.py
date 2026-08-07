"""The PM runtime on the brain host: scope -> ticket ->
crew intent -> summary, through governed tools only; a gateway deny
surfaces as a tool observation the model sees, never a crash."""

import pytest
from conftest import ScriptedLLM, calls, gateway, says, scripted, tc  # noqa: F401

from agent.brain import pm_dispatch
from agent.config import AgentSettings


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


def test_pm_files_ticket_and_records_intent(settings, gateway):
    llm = ScriptedLLM([
        calls(tc("read_issues")),
        calls(tc("create_issue", title="Implement slug endpoint",
                 body="acceptance: ...")),
        calls(tc("request_crew", crew_template="feature-delivery",
                 reason="ticket 7")),
        says("Set up: ticket 7, crew requested."),
    ])

    with scripted(gateway, llm):
        result = pm_dispatch(settings, "run-pm-1", "Ship a slug endpoint")

    assert result.error is None
    assert result.ticket_urls == ["https://github.test/i/7"]
    assert result.crew_request_id == "crq-abc123"
    tools = [tool for tool, _ in gateway.calls]
    assert tools == ["issues.read", "issues.create", "crew.request"]
    # The PM prompt is the PM's — the mock brain keys on it.
    messages, _ = llm.requests[0]
    from conftest import text_of
    assert "product manager" in text_of(messages[0])


def test_pm_deny_surfaces_as_tool_error_not_crash(settings, gateway):
    """Refusal is the gateway's: a PM told to exec sees the refusal as a
    tool result and can carry on scoping. `exec` is not even on the PM's
    tool surface — the SDK feeds back tool-not-found without any gateway
    call."""
    llm = ScriptedLLM([
        calls(tc("exec", cmd="rm -rf /")),  # not a PM tool at all
        calls(tc("create_issue", title="t", body="b")),
        says("done"),
    ])

    with scripted(gateway, llm):
        result = pm_dispatch(settings, "run-pm-1", "Ship a slug endpoint")

    assert result.error is None
    assert result.ticket_urls == ["https://github.test/i/7"]
    # The forbidden call never reached the gateway (not on the surface).
    assert all(tool != "sandbox.exec" for tool, _ in gateway.calls)


def test_pm_without_a_ticket_fails_honestly(settings, gateway):
    llm = ScriptedLLM([says("nothing to do here")])

    with scripted(gateway, llm):
        result = pm_dispatch(settings, "run-pm-1", "Ship a slug endpoint")

    assert result.error == "PM filed no ticket for the goal"
