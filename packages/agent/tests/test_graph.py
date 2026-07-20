"""S7 graph tests: plan -> act -> verify -> deliver against a scripted LLM
and a fake gateway (see conftest)."""

import pytest

from agent.graph import run_graph
from agent.toolbox import ACT_TOOLS, DELIVER_TOOLS
from conftest import (
    PR_URL,
    RUN_ID,
    TESTS_FAIL,
    FakeGateway,
    ScriptedLLM,
    calls,
    says,
    tc,
)

pytestmark = pytest.mark.anyio

TASK = "Add a `greet(name)` function to src/hello.py with a test."


def tool_names(specs: list[dict] | None) -> list[str]:
    return [spec["function"]["name"] for spec in specs or []]


# --- happy path ---------------------------------------------------------------


async def test_happy_path_reaches_deliver_and_reports_pr(settings, gateway):
    llm = ScriptedLLM(
        [
            says("Plan: 1. write greet 2. write test 3. run tests 4. deliver"),
            calls(tc("write_file", path="src/hello.py", content="def greet(n): ...")),
            says("Implementation done, ready for the test suite."),
            calls(
                tc(
                    "exec",
                    cmd=(
                        "git checkout -b feature/greet && git add -A && "
                        "git -c user.name=a -c user.email=a@p commit -m 'add greet'"
                    ),
                )
            ),
            calls(tc("open_pr", branch="feature/greet", title="Add greet", body="Adds greet().")),
        ]
    )

    result = await run_graph(llm, gateway, settings, RUN_ID, TASK)

    assert result.pr_url == PR_URL
    assert result.error is None
    assert result.iterations == 1
    assert llm.script == []

    # Full gateway trace: survey, edit, tests, commit, bundle, PR.
    assert gateway.tools_called() == [
        "sandbox.list_dir",
        "sandbox.write_file",
        "sandbox.run_tests",
        "sandbox.exec",
        "sandbox.git_bundle",
        "git.open_pr",
    ]
    # Every call carries the run_id (§4.2).
    assert all(args["run_id"] == RUN_ID for _, args in gateway.calls)

    open_pr_args = gateway.calls[-1][1]
    assert open_pr_args["repo"] == settings.target_repo
    assert open_pr_args["branch"] == "feature/greet"
    # The bundle came from sandbox.git_bundle; the model never saw the base64.
    assert open_pr_args["bundle_b64"] == "YnVuZGxl"


async def test_prompt_and_toolsets_per_phase(settings, gateway):
    llm = ScriptedLLM(
        [
            says("plan"),
            says("nothing to do"),
            calls(tc("open_pr", branch="feature/x", title="t", body="b")),
        ]
    )

    await run_graph(llm, gateway, settings, RUN_ID, TASK)

    plan_messages, plan_tools = llm.requests[0]
    assert plan_tools is None
    assert plan_messages[0]["role"] == "system"
    assert "feature/" in plan_messages[0]["content"]
    assert settings.target_repo in plan_messages[0]["content"]
    assert TASK in plan_messages[1]["content"]
    assert "README.md" in plan_messages[1]["content"]  # workspace survey

    _, act_tools = llm.requests[1]
    assert tool_names(act_tools) == ACT_TOOLS  # no open_pr while iterating

    deliver_messages, deliver_tools = llm.requests[2]
    assert tool_names(deliver_tools) == DELIVER_TOOLS
    assert "open the pull request" in deliver_messages[-1]["content"]


# --- verify loop and iteration cap ---------------------------------------------


async def test_failing_tests_loop_back_with_report_until_iteration_cap(settings, gateway):
    gateway.default_test_result = TESTS_FAIL
    # One act step per verify cycle; tests never pass, so the run fails
    # after max_iterations (3) verify cycles.
    llm = ScriptedLLM([says("plan"), says("try 1"), says("try 2"), says("try 3")])

    result = await run_graph(llm, gateway, settings, RUN_ID, TASK)

    assert result.pr_url is None
    assert result.iterations == settings.max_iterations
    assert result.error == "tests still failing after 3 iterations"
    assert gateway.tools_called().count("sandbox.run_tests") == 3
    assert "git.open_pr" not in gateway.tools_called()

    # The failure report was surfaced to the model between cycles.
    last_act_messages, _ = llm.requests[-1]
    assert "exit code 1" in last_act_messages[-1]["content"]
    assert "1 failed: test_hello" in last_act_messages[-1]["content"]


# --- deny path (E2 recovery) ----------------------------------------------------


async def test_denied_pr_is_surfaced_and_agent_retries_on_feature_branch(settings, gateway):
    llm = ScriptedLLM(
        [
            says("plan"),
            says("done"),
            # Injected-task behaviour: try to deliver straight to main.
            calls(tc("open_pr", branch="main", title="t", body="b")),
            # After seeing the denial, comply with policy.
            calls(tc("open_pr", branch="feature/greet", title="t", body="b")),
        ]
    )

    result = await run_graph(llm, gateway, settings, RUN_ID, TASK)

    assert result.pr_url == PR_URL
    assert result.error is None

    pr_attempts = [args["branch"] for tool, args in gateway.calls if tool == "git.open_pr"]
    assert pr_attempts == ["main", "feature/greet"]

    # The deny (with the rule name) was fed back to the model as a tool result.
    retry_messages, _ = llm.requests[-1]
    denial = retry_messages[-1]
    assert denial["role"] == "tool"
    assert denial["content"].startswith("ERROR:")
    assert "git_open_pr_protected_branch" in denial["content"]


async def test_deliver_without_pr_fails_the_run(settings, gateway):
    llm = ScriptedLLM([says("plan"), says("done"), says("I refuse to open a PR.")])

    result = await run_graph(llm, gateway, settings, RUN_ID, TASK)

    assert result.pr_url is None
    assert result.error == "agent stopped without opening a pull request"
