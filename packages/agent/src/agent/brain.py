"""The brain host: the OpenHands Agent SDK as a library
behind Poseidon contracts.

The SDK owns the reasoning loop, context machinery and coding heuristics —
nothing else. Enforced boundaries:

- Every side effect is a Hands tool resolving to a gateway MCP call
  (`agent.hands`); the host holds the only credential and the SDK's default
  tools are control flow only (finish/think). The OpenHands built-ins and
  its sub-agent machinery (`TaskToolSet`) are never installed or registered.
- The brain starts and stops no sandboxes and owns no process-level
  retries/timeouts/cancellation — the workflow dispatches into this host
  and waits (C6); `num_retries=1` keeps the SDK from grinding on errors the
  workflow owns (a `budget_exhausted` 429 surfaces immediately).
- The implement dispatch ends at a completion claim: its tool surface has
  no `open_pr`, its advisory verify (host-driven `sandbox.run_tests`
  between conversation turns) only shapes iteration, and delivery is the
  separate workflow-directed `mode="deliver"` dispatch after the platform
  verify gate.

The mode runners are synchronous — the SDK conversation loop is blocking —
and run in a worker thread under the async C6 app (`asyncio.to_thread`).
The `GatewayBridge` gives the executors a sync facade over the async MCP
client by parking the session on a dedicated event-loop thread.
"""

import asyncio
import tempfile
import threading
from concurrent.futures import Future
from pathlib import Path
from typing import Any

from openhands.sdk import (
    LLM,
    Agent,
    Conversation,
    ConversationExecutionStatus,
    LLMSummarizingCondenser,
)
from openhands.sdk.llm import Message, TextContent
from pydantic import BaseModel, SecretStr

from agent.config import AgentSettings
from agent.gateway_client import McpGateway, ToolCallError
from agent.hands import (
    ACT_TOOLS,
    DELIVER_TOOLS,
    PM_TOOLS,
    REVIEWER_TOOLS,
    HandsContext,
    ToolRecords,
    hands,
)
from common.telemetry import get_logger

SYSTEM_PROMPT_PATH = Path(__file__).parent / "prompts" / "system.md"
PM_PROMPT_PATH = Path(__file__).parent / "prompts" / "pm.md"
# The PM's whole dispatch is a handful of tool calls; the cap is a backstop.
PM_STEP_CAP = 10
# SDK steps allowed within one act cycle before tests are forced to run.
ACT_STEP_CAP = 20
# Condensation is real context machinery for a real model; the sizes keep it
# far out of reach of the short deterministic e2e conversations.
CONDENSER_MAX_SIZE = 120
CONDENSER_KEEP_FIRST = 4
_OUTPUT_CLIP = 4000

log = get_logger("agent.brain")


class RunResult(BaseModel):
    pr_url: str | None = None
    iterations: int = 0
    error: str | None = None
    # Planner mode: the task brief the executor's
    # dispatch carries verbatim.
    brief: str | None = None
    # PM runtime: the tickets it filed and the crew intent
    # it recorded — what an intake run links to the run record.
    ticket_urls: list[str] = []
    crew_request_id: str | None = None
    # Reviewer mode: the findings comment(s) it posted —
    # the review workflow refuses to reach the merge gate without one.
    comment_urls: list[str] = []


class GatewayBridge:
    """Sync facade over the async MCP gateway session for the SDK's
    synchronous tool executors. The session lives on its own event-loop
    thread, opened and closed inside ONE long-lived task there — the MCP
    client's anyio cancel scopes are task-bound, so entering and exiting
    the session from separately scheduled coroutines corrupts them.
    `call` blocks the conversation thread on a per-call coroutine (MCP
    sessions handle concurrent requests; their per-call scopes open and
    close within that same coroutine) — one call at a time, matching the
    SDK's tool concurrency."""

    # MCP client read timeout is 660 s (gateway_client); this is the backstop.
    CALL_TIMEOUT_S = 700

    def __init__(self, url: str, token: str) -> None:
        self._gateway = McpGateway(url, token)
        self._loop = asyncio.new_event_loop()
        self._thread = threading.Thread(
            target=self._loop.run_forever, name="gateway-bridge", daemon=True
        )
        self._close_event: asyncio.Event | None = None
        self._ready: Future[None] = Future()
        self._session: Future[Any] | None = None

    async def _run_session(self) -> None:
        try:
            async with self._gateway:
                self._close_event = asyncio.Event()
                self._ready.set_result(None)
                await self._close_event.wait()
        except BaseException as exc:
            if not self._ready.done():
                self._ready.set_exception(exc)
            raise

    def __enter__(self) -> "GatewayBridge":
        self._thread.start()
        self._session = asyncio.run_coroutine_threadsafe(self._run_session(), self._loop)
        self._ready.result(timeout=60)
        return self

    def __exit__(self, *exc_info: Any) -> None:
        try:
            if self._close_event is not None and self._session is not None:
                self._loop.call_soon_threadsafe(self._close_event.set)
                self._session.result(timeout=30)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            self._thread.join(timeout=5)
            self._loop.close()

    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        return asyncio.run_coroutine_threadsafe(
            self._gateway.call(tool, args), self._loop
        ).result(self.CALL_TIMEOUT_S)


def build_llm(settings: AgentSettings, run_id: str, token: str) -> LLM:
    """The LLM gateway client, SDK-shaped: authenticated
    with the workload credential, every call attributed to its run via the
    header. The gateway routes the model by tier — the name here is
    advisory. litellm needs a provider prefix to route to the custom
    base_url; the wire model name stays the bare setting."""
    model = settings.model if "/" in settings.model else f"openai/{settings.model}"
    return LLM(
        model=model,
        base_url=settings.llm_proxy_url,
        api_key=SecretStr(token),
        extra_headers={"X-Poseidon-Run-Id": run_id},
        num_retries=1,  # the workflow owns retries; budget 429s surface now
        usage_id=run_id,
    )


def _build_conversation(
    llm: LLM,
    system_prompt: str,
    tool_names: list[str],
    ctx: HandsContext,
    workspace: str,
    max_steps: int,
) -> Conversation:
    agent = Agent(
        llm=llm,
        tools=hands(tool_names),
        system_prompt=system_prompt,
        condenser=LLMSummarizingCondenser(
            llm=llm, max_size=CONDENSER_MAX_SIZE, keep_first=CONDENSER_KEEP_FIRST
        ),
    )
    # The PoseidonHands factory reads this when the SDK lazily resolves
    # tools (on a worker thread, at first send_message).
    ctx.activate()
    return Conversation(
        agent=agent,
        workspace=workspace,
        visualizer=None,
        stuck_detection=False,  # deterministic scripts and short loops
        max_iteration_per_run=max_steps,
    )


def _render_prompt(path: Path, settings: AgentSettings) -> str:
    """Literal placeholder substitution — deliberately not str.format:
    format machinery turns any other brace in the prompt text into a
    crash (or a format-spec evaluation), and prompt files are edited as
    prose."""
    return path.read_text().replace("{target_repo}", settings.target_repo)


def _system_prompt(settings: AgentSettings) -> str:
    return _render_prompt(SYSTEM_PROMPT_PATH, settings)


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


def _survey_entries(bridge: GatewayBridge, run_id: str) -> str:
    survey = bridge.call("sandbox.list_dir", {"run_id": run_id, "path": "."})
    return ", ".join(
        f"{e['name']}{'/' if e['type'] == 'dir' else ''}" for e in survey["entries"]
    )


def _task_prompt(task_text: str, entries: str) -> str:
    return (
        f"Task:\n{task_text}\n\n"
        f"Workspace root entries: {entries}\n\n"
        "Write a short numbered plan for completing this task, "
        "then carry it out."
    )


def implement_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """mode=implement: the SDK loop acts (read/write/list/exec, never
    open_pr), the host runs the ADVISORY verify between turns, and advisory
    green ends the dispatch with a completion claim. The gate of record is
    the platform's own `sandbox.run_tests` in the implement workflow."""
    token = settings.resolve_gateway_token()
    records = ToolRecords()
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        ctx = HandsContext(bridge, settings, run_id, records)
        with tempfile.TemporaryDirectory(prefix="brain-") as workspace:
            convo = _build_conversation(
                build_llm(settings, run_id, token),
                _system_prompt(settings),
                ACT_TOOLS,
                ctx,
                workspace,
                max_steps=ACT_STEP_CAP,
            )
            try:
                convo.send_message(
                    _task_prompt(task_text, _survey_entries(bridge, run_id))
                )
                iterations = 0
                while True:
                    convo.run()
                    if convo.state.execution_status == ConversationExecutionStatus.ERROR:
                        return RunResult(
                            iterations=iterations,
                            error=f"agent loop failed after {iterations} verify cycles",
                        )
                    iterations += 1
                    try:
                        result = bridge.call("sandbox.run_tests", {"run_id": run_id})
                    except ToolCallError as exc:
                        result = {"exit_code": -1, "stdout": "", "stderr": str(exc)}
                    passed = result.get("exit_code") == 0
                    log.info("verify", extra={"iteration": iterations, "passed": passed})
                    if passed:
                        # Advisory green = the completion claim (sequencing
                        # choice 8): the platform verifies of record and the
                        # workflow directs delivery in a separate dispatch.
                        return RunResult(iterations=iterations)
                    if iterations >= settings.max_iterations:
                        return RunResult(
                            iterations=iterations,
                            error=(f"tests still failing after {iterations} iterations"),
                        )
                    convo.send_message(_test_report(result) + "\n\nFix the failures.")
            finally:
                convo.close()


def deliver_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """The workflow-directed delivery dispatch:
    only ever dispatched after the platform verify gate passed.
    Commit the sandbox's work on the task's branch, bundle it, open the PR.
    A policy denial comes back as the tool observation so the model can
    recover (the E2 path)."""
    token = settings.resolve_gateway_token()
    records = ToolRecords()
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        ctx = HandsContext(bridge, settings, run_id, records)
        with tempfile.TemporaryDirectory(prefix="brain-") as workspace:
            convo = _build_conversation(
                build_llm(settings, run_id, token),
                _system_prompt(settings),
                DELIVER_TOOLS,
                ctx,
                workspace,
                # One SDK step is one delivery attempt (commit + open_pr);
                # the injected scenario must fit attempt-deny-recover.
                max_steps=max(settings.max_iterations, 4),
            )
            try:
                convo.send_message(
                    f"Task:\n{task_text}\n\n"
                    "The platform has verified your work in the sandbox: the "
                    "test suite is green. Deliver it now — commit your changes "
                    "on the task's feature/<slug> branch and open the pull "
                    "request with the open_pr tool."
                )
                convo.run()
            finally:
                convo.close()
    if records.pr_url is None:
        return RunResult(
            error="agent stopped without opening a pull request", iterations=1
        )
    log.info("delivered", extra={"pr_url": records.pr_url})
    return RunResult(pr_url=records.pr_url, iterations=1)


def review_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """The ephemeral reviewer's dispatch: read the
    diff via git.read, execute the suite in the run's sandbox (provisioned
    from the PR ref), post findings via git.comment. Read-and-judge
    only: no write_file, no open_pr, and the compiled sdet scope enforces
    the same at the gateway. A dispatch with no posted findings is a failed
    review."""
    token = settings.resolve_gateway_token()
    records = ToolRecords()
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        ctx = HandsContext(bridge, settings, run_id, records)
        with tempfile.TemporaryDirectory(prefix="brain-") as workspace:
            convo = _build_conversation(
                build_llm(settings, run_id, token),
                _system_prompt(settings),
                REVIEWER_TOOLS,
                ctx,
                workspace,
                max_steps=settings.max_iterations * 2,
            )
            try:
                convo.send_message(f"Task:\n{task_text}")
                convo.run()
            finally:
                convo.close()
    if not records.comment_urls:
        return RunResult(error="reviewer posted no findings")
    log.info("review finished", extra={"comments": records.comment_urls})
    return RunResult(comment_urls=records.comment_urls)


def pm_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """The PM runtime: scope a goal, file tickets, record
    crew intent — through governed tools only. No sandbox, no delivery, no
    private memory: the backlog and the run history are the PM's only
    cross-run state. Executor-tool refusal is the
    gateway's job; a deny surfaces as a tool observation the model sees."""
    token = settings.resolve_gateway_token()
    records = ToolRecords()
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        ctx = HandsContext(bridge, settings, run_id, records)
        with tempfile.TemporaryDirectory(prefix="brain-") as workspace:
            convo = _build_conversation(
                build_llm(settings, run_id, token),
                _render_prompt(PM_PROMPT_PATH, settings),
                PM_TOOLS,
                ctx,
                workspace,
                max_steps=PM_STEP_CAP,
            )
            try:
                convo.send_message(f"Goal:\n{task_text}")
                convo.run()
                capped = convo.state.execution_status == ConversationExecutionStatus.ERROR
            finally:
                convo.close()
    if capped:
        return RunResult(
            error=f"PM did not finish within {PM_STEP_CAP} steps",
            ticket_urls=records.ticket_urls,
            crew_request_id=records.crew_request_id,
        )
    if not records.ticket_urls:
        return RunResult(error="PM filed no ticket for the goal")
    log.info(
        "pm finished",
        extra={"tickets": records.ticket_urls, "crew_request": records.crew_request_id},
    )
    return RunResult(
        ticket_urls=records.ticket_urls, crew_request_id=records.crew_request_id
    )


def plan_dispatch(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    gateway_token: str | None = None,
) -> RunResult:
    """Planner mode: survey the
    run's sandbox read-only, produce the task brief, write nothing. The
    planner is LLM-only — one completion, no tool loop — and calls the
    gateway under its *own* identity (the per-dispatch token), so its
    compiled read-only scope is what policy enforces."""
    token = gateway_token or settings.resolve_gateway_token()
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        entries = _survey_entries(bridge, run_id)
    llm = build_llm(settings, run_id, token)
    response = llm.completion(
        messages=[
            Message(role="system", content=[TextContent(text=_system_prompt(settings))]),
            Message(
                role="user", content=[TextContent(text=_task_prompt(task_text, entries))]
            ),
        ]
    )
    brief = "".join(
        block.text for block in response.message.content if isinstance(block, TextContent)
    ).strip()
    if not brief:
        return RunResult(error="planner produced an empty brief")
    return RunResult(brief=brief)
