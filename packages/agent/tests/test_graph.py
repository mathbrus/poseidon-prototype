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


async def test_happy_path_ends_in_a_completion_claim(settings, gateway):
    """Sequencing choice 8 (plan step 19): the implement dispatch ends at
    the advisory-green completion claim — no PR, no deliver tools, ever."""
    llm = ScriptedLLM(
        [
            says("Plan: 1. write greet 2. write test 3. run tests"),
            calls(tc("write_file", path="src/hello.py", content="def greet(n): ...")),
            says("Implementation done, ready for the test suite."),
        ]
    )

    result = await run_graph(llm, gateway, settings, RUN_ID, TASK)

    assert result.error is None
    assert result.pr_url is None  # the brain never self-delivers
    assert result.iterations == 1
    assert llm.script == []
    assert gateway.tools_called() == [
        "sandbox.list_dir",
        "sandbox.write_file",
        "sandbox.run_tests",
    ]
    assert all(args["run_id"] == RUN_ID for _, args in gateway.calls)


async def test_deliver_dispatch_commits_bundles_and_opens_pr(settings, gateway):
    """The workflow-directed delivery (mode=deliver): commit, bundle, PR."""
    from agent.graph import run_deliver
    import agent.graph as graph_module

    llm = ScriptedLLM(
        [
            calls(
                tc(
                    "exec",
                    cmd=(
                        "git checkout -b feature/greet && git add -A && "
                        "git -c user.name=a -c user.email=a@p commit -m 'add greet'"
                    ),
                ),
                tc("open_pr", branch="feature/greet", title="Add greet",
                   body="Adds greet()."),
            ),
        ]
    )

    class FakeGatewayCtx:
        async def __aenter__(self):
            return gateway
        async def __aexit__(self, *exc):
            return False

    original_llm = graph_module.OpenAICompatibleLLM
    original_gw = graph_module.McpGateway
    graph_module.OpenAICompatibleLLM = lambda *a, **k: llm
    graph_module.McpGateway = lambda *a, **k: FakeGatewayCtx()
    try:
        result = await run_deliver(settings, RUN_ID, TASK)
    finally:
        graph_module.OpenAICompatibleLLM = original_llm
        graph_module.McpGateway = original_gw

    assert result.pr_url == PR_URL
    assert result.error is None
    assert gateway.tools_called() == [
        "sandbox.exec", "sandbox.git_bundle", "git.open_pr",
    ]
    open_pr_args = gateway.calls[-1][1]
    assert open_pr_args["repo"] == settings.target_repo
    assert open_pr_args["branch"] == "feature/greet"
    # The bundle came from sandbox.git_bundle; the model never saw the base64.
    assert open_pr_args["bundle_b64"] == "YnVuZGxl"


async def test_prompt_and_toolsets_per_phase(settings, gateway):
    """The implement dispatch never offers delivery tools (choice 8):
    plan has none, act has the read/write set, and the graph ends at the
    claim — open_pr exists only in the separate deliver dispatch."""
    llm = ScriptedLLM([says("plan"), says("nothing to do")])

    await run_graph(llm, gateway, settings, RUN_ID, TASK)

    plan_messages, plan_tools = llm.requests[0]
    assert plan_tools is None
    assert plan_messages[0]["role"] == "system"
    assert "feature/" in plan_messages[0]["content"]
    assert settings.target_repo in plan_messages[0]["content"]
    assert TASK in plan_messages[1]["content"]
    assert "README.md" in plan_messages[1]["content"]  # workspace survey

    _, act_tools = llm.requests[1]
    assert tool_names(act_tools) == ACT_TOOLS  # no open_pr, ever, here
    assert len(llm.requests) == 2


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


def _deliver(settings, gateway, llm):
    import agent.graph as graph_module
    from agent.graph import run_deliver

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
            return await run_deliver(settings, RUN_ID, TASK)
        finally:
            graph_module.OpenAICompatibleLLM = original_llm
            graph_module.McpGateway = original_gw

    return go()


async def test_denied_pr_is_surfaced_and_agent_retries_on_feature_branch(settings, gateway):
    """E2 in the deliver dispatch: the deny (with the rule name) feeds back
    and the model recovers onto feature/*."""
    llm = ScriptedLLM(
        [
            # Injected-task behaviour: try to deliver straight to main.
            calls(tc("open_pr", branch="main", title="t", body="b")),
            # After seeing the denial, comply with policy.
            calls(tc("open_pr", branch="feature/greet", title="t", body="b")),
        ]
    )

    result = await _deliver(settings, gateway, llm)

    assert result.pr_url == PR_URL
    assert result.error is None

    pr_attempts = [args["branch"] for tool, args in gateway.calls if tool == "git.open_pr"]
    assert pr_attempts == ["main", "feature/greet"]

    retry_messages, _ = llm.requests[-1]
    denial = retry_messages[-1]
    assert denial["role"] == "tool"
    assert denial["content"].startswith("ERROR:")
    assert "git_open_pr_protected_branch" in denial["content"]


async def test_deliver_without_pr_fails_the_run(settings, gateway):
    llm = ScriptedLLM([says("I refuse to open a PR.")])

    result = await _deliver(settings, gateway, llm)

    assert result.pr_url is None
    assert result.error == "agent stopped without opening a pull request"
