"""LangGraph loop: plan -> act -> verify -> deliver (§4.2).

- plan: survey the workspace root and produce a short step plan.
- act: LLM tool-calling steps (read/write/list/exec) until the model stops
  requesting tools (or the per-cycle step cap forces a verify).
- verify: run the test suite via the gateway; loop back to act on failure,
  up to MAX_ITERATIONS verify cycles.
- deliver: LLM tool-calling steps with `open_pr` available; ends when a PR
  is open. A policy denial (e.g. attempting to deliver `main`) is fed back
  to the model as the tool result so it can recover onto `feature/*` (E2).

Every tool call goes through the gateway MCP client; the graph itself holds
no credentials and touches nothing but the gateway.
"""

from pathlib import Path
from typing import Any, Literal, TypedDict

from langgraph.graph import END, StateGraph
from pydantic import BaseModel

from agent.config import AgentSettings
from agent.gateway_client import Gateway, McpGateway, ToolCallError
from agent.llm import LLM, Completion, OpenAICompatibleLLM, ToolCall, assistant_message
from agent.toolbox import (
    ACT_TOOLS,
    DELIVER_TOOLS,
    Toolbox,
    tool_result_message,
    tool_specs,
)
from common.telemetry import get_logger

SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
# LLM steps allowed within one act cycle before tests are forced to run.
ACT_STEP_CAP = 20
_OUTPUT_CLIP = 4000


class AgentState(TypedDict):
    task_text: str
    messages: list[dict[str, Any]]
    iterations: int  # completed verify cycles
    act_steps: int  # LLM steps since the last verify
    deliver_steps: int
    pr_url: str | None
    error: str | None
    next: str


class RunResult(BaseModel):
    pr_url: str | None = None
    iterations: int = 0
    error: str | None = None
    # Planner mode (plan step 16d): the §4.4 task brief the executor's
    # dispatch carries verbatim.
    brief: str | None = None


def _clip(text: str, limit: int = _OUTPUT_CLIP) -> str:
    if len(text) <= limit:
        return text
    return f"[... {len(text) - limit} chars clipped ...]\n" + text[-limit:]


def _test_report(result: dict[str, Any]) -> str:
    return (
        f"Test run finished with exit code {result.get('exit_code')}.\n"
        f"stdout:\n{_clip(str(result.get('stdout', '')))}\n"
        f"stderr:\n{_clip(str(result.get('stderr', '')))}"
    )


def build_graph(llm: LLM, toolbox: Toolbox, settings: AgentSettings):
    log = get_logger("agent.graph")
    system_prompt = SYSTEM_PROMPT_PATH.read_text().format(
        target_repo=settings.target_repo
    )

    async def run_tool_calls(
        completion: Completion, messages: list[dict[str, Any]], allowed: list[str]
    ) -> dict[str, dict[str, Any]]:
        """Execute the completion's tool calls, appending one tool message each.

        Returns successful results by tool name; errors (including policy
        denials) become error-text tool messages so the model sees them.
        Only tools offered in the current phase run — the model sometimes
        calls tools it was not given (e.g. `open_pr` while still acting).
        """
        results: dict[str, dict[str, Any]] = {}
        for call in completion.tool_calls:
            if call.name not in allowed:
                log.info("tool call rejected", extra={"tool": call.name})
                messages.append(
                    tool_result_message(
                        call, f"ERROR: tool {call.name} is not available right now"
                    )
                )
                continue
            try:
                result = await toolbox.execute(call)
            except ToolCallError as exc:
                log.info("tool call failed", extra={"tool": call.name, "error": str(exc)})
                messages.append(tool_result_message(call, f"ERROR: {exc}"))
                continue
            results[call.name] = result
            messages.append(tool_result_message(call, result))
        return results

    async def plan(state: AgentState) -> dict[str, Any]:
        survey = await toolbox.execute(
            ToolCall(id="plan-survey", name="list_dir", arguments={"path": "."})
        )
        entries = ", ".join(
            f"{e['name']}{'/' if e['type'] == 'dir' else ''}" for e in survey["entries"]
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Task:\n{state['task_text']}\n\n"
                    f"Workspace root entries: {entries}\n\n"
                    "Write a short numbered plan for completing this task, "
                    "then carry it out."
                ),
            },
        ]
        completion = await llm.complete(messages)
        messages.append(assistant_message(completion))
        log.info("planned", extra={"plan": _clip(completion.content or "", 500)})
        return {"messages": messages, "next": "act"}

    async def act(state: AgentState) -> dict[str, Any]:
        messages = list(state["messages"])
        completion = await llm.complete(messages, tools=tool_specs(ACT_TOOLS))
        messages.append(assistant_message(completion))
        await run_tool_calls(completion, messages, ACT_TOOLS)
        act_steps = state["act_steps"] + 1
        done_acting = not completion.tool_calls or act_steps >= ACT_STEP_CAP
        return {
            "messages": messages,
            "act_steps": act_steps,
            "next": "verify" if done_acting else "act",
        }

    async def verify(state: AgentState) -> dict[str, Any]:
        iterations = state["iterations"] + 1
        try:
            result = await toolbox.run_tests()
        except ToolCallError as exc:
            result = {"exit_code": -1, "stdout": "", "stderr": str(exc)}
        passed = result.get("exit_code") == 0
        log.info("verify", extra={"iteration": iterations, "passed": passed})
        update: dict[str, Any] = {"iterations": iterations, "act_steps": 0}
        if passed:
            return {**update, "next": "deliver"}
        if iterations >= settings.max_iterations:
            return {
                **update,
                "error": f"tests still failing after {iterations} iterations",
                "next": "end",
            }
        messages = list(state["messages"])
        messages.append(
            {"role": "user", "content": _test_report(result) + "\n\nFix the failures."}
        )
        return {**update, "messages": messages, "next": "act"}

    async def deliver(state: AgentState) -> dict[str, Any]:
        messages = list(state["messages"])
        if state["deliver_steps"] == 0:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        "The test suite passes. Deliver your work now: commit your "
                        "changes on a feature/<slug> branch and open the pull "
                        "request with the open_pr tool."
                    ),
                }
            )
        completion = await llm.complete(messages, tools=tool_specs(DELIVER_TOOLS))
        messages.append(assistant_message(completion))
        results = await run_tool_calls(completion, messages, DELIVER_TOOLS)
        deliver_steps = state["deliver_steps"] + 1
        update: dict[str, Any] = {"messages": messages, "deliver_steps": deliver_steps}

        pr_url = results.get("open_pr", {}).get("pr_url")
        if pr_url:
            log.info("delivered", extra={"pr_url": pr_url})
            return {**update, "pr_url": pr_url, "next": "end"}
        if not completion.tool_calls:
            return {
                **update,
                "error": "agent stopped without opening a pull request",
                "next": "end",
            }
        if deliver_steps >= settings.max_iterations:
            return {
                **update,
                "error": f"no pull request after {deliver_steps} delivery steps",
                "next": "end",
            }
        return {**update, "next": "deliver"}

    graph = StateGraph(AgentState)
    graph.add_node("plan", plan)
    graph.add_node("act", act)
    graph.add_node("verify", verify)
    graph.add_node("deliver", deliver)
    graph.set_entry_point("plan")
    graph.add_edge("plan", "act")
    route = lambda state: state["next"]  # noqa: E731
    graph.add_conditional_edges("act", route, {"act": "act", "verify": "verify"})
    graph.add_conditional_edges(
        "verify", route, {"act": "act", "deliver": "deliver", "end": END}
    )
    graph.add_conditional_edges("deliver", route, {"deliver": "deliver", "end": END})
    return graph.compile()


def initial_state(task_text: str) -> AgentState:
    return AgentState(
        task_text=task_text,
        messages=[],
        iterations=0,
        act_steps=0,
        deliver_steps=0,
        pr_url=None,
        error=None,
        next="plan",
    )


async def run_graph(
    llm: LLM, gateway: Gateway, settings: AgentSettings, run_id: str, task_text: str
) -> RunResult:
    """Run the loop to completion against an already-open gateway."""
    toolbox = Toolbox(gateway, settings, run_id)
    graph = build_graph(llm, toolbox, settings)
    # In-graph caps terminate well before this; the limit is a hard backstop.
    recursion_limit = 10 + (ACT_STEP_CAP + 2) * settings.max_iterations
    state = await graph.ainvoke(
        initial_state(task_text), config={"recursion_limit": recursion_limit}
    )
    return RunResult(
        pr_url=state["pr_url"], iterations=state["iterations"], error=state["error"]
    )


async def run_task(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """Entry point used by the FastAPI app: real LLM proxy + real gateway."""
    llm = OpenAICompatibleLLM(
        settings.llm_proxy_url, settings.model,
        token=settings.resolve_gateway_token(), run_id=run_id,
    )
    async with McpGateway(settings.mcp_gateway_url, settings.resolve_gateway_token()) as gateway:
        return await run_graph(llm, gateway, settings, run_id, task_text)


async def run_plan(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    gateway_token: str | None = None,
) -> RunResult:
    """Planner mode (§4.4 planner contract, plan step 16d): survey the
    run's sandbox read-only, produce the task brief, write nothing. The
    planner is LLM-only — it borrows this brain host for the dispatch and
    calls the gateway under its *own* identity (the per-dispatch token),
    so its compiled read-only scope is what policy enforces."""
    token = gateway_token or settings.resolve_gateway_token()
    llm = OpenAICompatibleLLM(
        settings.llm_proxy_url, settings.model, token=token, run_id=run_id
    )
    system_prompt = SYSTEM_PROMPT_PATH.read_text().format(
        target_repo=settings.target_repo
    )
    async with McpGateway(settings.mcp_gateway_url, token) as gateway:
        toolbox = Toolbox(gateway, settings, run_id)
        survey = await toolbox.execute(
            ToolCall(id="plan-survey", name="list_dir", arguments={"path": "."})
        )
        entries = ", ".join(
            f"{e['name']}{'/' if e['type'] == 'dir' else ''}"
            for e in survey["entries"]
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {
                "role": "user",
                "content": (
                    f"Task:\n{task_text}\n\n"
                    f"Workspace root entries: {entries}\n\n"
                    "Write a short numbered plan for completing this task, "
                    "then carry it out."
                ),
            },
        ]
        completion = await llm.complete(messages)
        brief = (completion.content or "").strip()
        if not brief:
            return RunResult(error="planner produced an empty brief")
        return RunResult(brief=brief)
