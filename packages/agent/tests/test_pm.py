"""The PM's scope procedure on the agent host: observation -> ticket ->
summary comment on the ticket, through governed tools only; a gateway deny
surfaces as a tool observation the model sees, never a crash."""

import pytest
from conftest import ISSUE_COMMENT_URL, ScriptedLLM, calls, says, scripted, tc, text_of

from agent.config import AgentSettings
from agent.procedures import run_scope

SUMMARY = "Set up: ticket 7 for the feature-delivery crew."


@pytest.fixture
def settings(tmp_path) -> AgentSettings:
    token = tmp_path / "token"
    token.write_text("pm-projected")
    return AgentSettings(
        mcp_gateway_url="http://gateway.test:8080",
        llm_proxy_url="http://llm.test:4000",
        model="mock-brain",
        role="pm",
        gateway_token_file=str(token),
        target_repo="github.test/example/target",
        vision="ship a tiny SaaS under governance",
    )


def test_pm_files_ticket_and_posts_its_summary(settings, gateway):
    llm = ScriptedLLM(
        [
            calls(tc("read_issues")),
            calls(
                tc(
                    "create_issue",
                    title="Implement slug endpoint",
                    body="acceptance: ...",
                    rationale="the strategy's first slice; nothing covers it yet",
                )
            ),
            calls(tc("comment_issue", number=7, body=SUMMARY)),
            says("Done."),
        ]
    )

    with scripted(gateway, llm):
        result = run_scope(settings, "run-pm-1", "Strategy:\nShip a slug endpoint")

    assert result.error is None
    assert result.ticket_urls == ["https://github.test/i/7"]
    assert result.comment_urls == [ISSUE_COMMENT_URL]
    tools = [tool for tool, _ in gateway.calls]
    assert tools == ["issues.read", "issues.create", "issues.comment"]
    # The summary lands on the ticket the PM filed, under the run's identity;
    # the number is the one create_issue's reply named.
    _, comment_args = gateway.calls[-1]
    assert comment_args == {
        "run_id": "run-pm-1",
        "repo": "github.test/example/target",
        "number": 7,
        "body": SUMMARY,
    }
    assert "comment_issue" in llm.offered(0)
    # The PM prompt is the PM's — the mock brain keys on it.
    messages, _ = llm.requests[0]
    assert "product manager" in text_of(messages[0])


def test_pm_deny_surfaces_as_tool_error_not_crash(settings, gateway):
    """Refusal is the gateway's: a PM told to exec sees the refusal as a
    tool result and can carry on scoping. `exec` is not even on the PM's
    tool surface — the SDK feeds back tool-not-found without any gateway
    call."""
    llm = ScriptedLLM(
        [
            calls(tc("exec", cmd="rm -rf /")),  # not a PM tool at all
            calls(tc("create_issue", title="t", body="b", rationale="r")),
            calls(tc("comment_issue", number=7, body="Filed t.")),
            says("done"),
        ]
    )

    with scripted(gateway, llm):
        result = run_scope(settings, "run-pm-1", "Strategy:\nShip a slug endpoint")

    assert result.error is None
    assert result.ticket_urls == ["https://github.test/i/7"]
    # The forbidden call never reached the gateway (not on the surface).
    assert all(tool != "sandbox.exec" for tool, _ in gateway.calls)


def test_pm_with_nothing_to_scope_finishes_clean(settings, gateway):
    """A delivered strategy is a valid state: filing nothing is not a
    failure, and the run record carries no tickets."""
    llm = ScriptedLLM([says("The strategy names no further slice.")])

    with scripted(gateway, llm):
        result = run_scope(settings, "run-pm-1", "Strategy:\nAll slices delivered.")

    assert result.error is None
    assert result.ticket_urls == [] and result.comment_urls == []


def test_pm_that_files_but_never_summarizes_fails_honestly(settings, gateway):
    """A ticket without its summary comment is an unfinished dispatch. The
    ticket is still reported, so the run record keeps its link."""
    llm = ScriptedLLM(
        [
            calls(tc("create_issue", title="t", body="b", rationale="r")),
            says("All set."),
        ]
    )

    with scripted(gateway, llm):
        result = run_scope(settings, "run-pm-1", "Strategy:\nShip a slug endpoint")

    assert result.error == "PM filed tickets without posting its summary"
    assert result.ticket_urls == ["https://github.test/i/7"]
    assert result.comment_urls == []


def test_pm_summary_comment_denied_is_a_tool_error_then_an_honest_failure(
    settings, gateway
):
    """A blueprint that withholds issues.comment from the PM: the deny is
    the observation the model sees (audited at the gateway), and with no
    comment posted the dispatch ends with the missing-summary error."""
    gateway.deny = {"issues.comment"}
    llm = ScriptedLLM(
        [
            calls(tc("create_issue", title="t", body="b", rationale="r")),
            calls(tc("comment_issue", number=7, body="Filed t.")),
            says("The platform refused the comment."),
        ]
    )

    with scripted(gateway, llm):
        result = run_scope(settings, "run-pm-1", "Strategy:\nShip a slug endpoint")

    assert result.error == "PM filed tickets without posting its summary"
    assert result.ticket_urls == ["https://github.test/i/7"]
    assert [tool for tool, _ in gateway.calls] == ["issues.create", "issues.comment"]
    # The deny reached the model as the tool's observation, rule name and all.
    last_messages, _ = llm.requests[-1]
    assert "default_deny" in text_of(last_messages[-1])
