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
  and waits; `num_retries=1` keeps the SDK from grinding on errors the
  workflow owns (a `budget_exhausted` 429 surfaces immediately).
- The implement dispatch ends at a completion claim: its tool surface has
  no `open_pr`, its advisory verify (host-driven `sandbox.run_tests`
  between conversation turns) only shapes iteration, and delivery is the
  separate workflow-directed `mode="deliver"` dispatch after the platform
  verify gate.

The mode runners are synchronous — the SDK conversation loop is blocking —
and run in a worker thread under the async dispatch app (`asyncio.to_thread`).
The `GatewayBridge` gives the executors a sync facade over the async MCP
client by parking the session on a dedicated event-loop thread.
"""

import asyncio
import re
import tempfile
import threading
from collections.abc import Iterator
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from openhands.sdk import (
    LLM,
    Agent,
    Conversation,
    ConversationExecutionStatus,
    LLMSummarizingCondenser,
)
from openhands.sdk.context import AgentContext
from openhands.sdk.conversation import get_agent_final_response
from openhands.sdk.llm import Message, TextContent
from openhands.sdk.skills import Skill
from pydantic import BaseModel, SecretStr

from agent.config import AgentSettings
from agent.gateway_client import McpGateway, ToolCallError
from agent.hands import (
    ACT_TOOLS,
    DELIVER_TOOLS,
    PLAN_TOOLS,
    PM_TOOLS,
    REVIEWER_TOOLS,
    HandsContext,
    ToolRecords,
    hands,
)
from agent.prompting import CODING_SECTIONS, MODEL_SECTIONS, READING_SECTIONS, compose
from common.telemetry import get_logger

# One prompt file per dispatch mode: each phase is told what it actually
# is, and the operating procedure matches the tool surface it is offered.
# The SDK's heuristic sections follow the procedure, chosen by what the
# mode does with the workspace.
PROMPTS_DIR = Path(__file__).parent / "prompts"
PROMPT_SECTIONS = {
    "implement": CODING_SECTIONS,
    "deliver": CODING_SECTIONS,
    "review": READING_SECTIONS,
    "plan": READING_SECTIONS,
    "pm": MODEL_SECTIONS,
    "decide": MODEL_SECTIONS,
}
# The PM's whole dispatch is a handful of tool calls; the cap is a backstop.
PM_STEP_CAP = 10
# SDK steps allowed within one act cycle before tests are forced to run.
ACT_STEP_CAP = 20
# The planner reads a handful of files and answers; the cap keeps a
# survey from becoming an implementation.
PLAN_STEP_CAP = 8
# Condensation is real context machinery for a real model; the sizes keep it
# far out of reach of the short deterministic e2e conversations.
CONDENSER_MAX_SIZE = 120
CONDENSER_KEEP_FIRST = 4
_OUTPUT_CLIP = 4000
# The repository's own instructions to agents, read from the sandbox and
# shown to the model as untrusted context. Bounded: a file can be anything.
REPO_INSTRUCTIONS_FILE = "AGENTS.md"
REPO_INSTRUCTIONS_CLIP = 8000

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
    # Reviewer mode: the findings comment(s) it posted, and the structured
    # verdict — pass/fail plus a one-line summary. The review workflow
    # refuses to reach the merge gate without a verdict; what the verdict
    # releases is compiled policy's decision, never the reviewer's.
    comment_urls: list[str] = []
    verdict: str | None = None
    verdict_summary: str | None = None
    # Decide mode: {action, target, rationale} — the conduct heartbeat's
    # chosen next action. The PM proposes; the platform validates,
    # authorizes and starts.
    decision: dict[str, Any] | None = None


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
    agent_context: AgentContext | None = None,
) -> Conversation:
    agent = Agent(
        llm=llm,
        tools=hands(tool_names),
        system_prompt=system_prompt,
        agent_context=agent_context,
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
        stuck_detection=True,
        max_iteration_per_run=max_steps,
    )


def _loop_failure(convo: Conversation, steps: int) -> str | None:
    """Why the SDK loop stopped short, or None if it finished. The SDK
    sets ERROR at the step cap and STUCK when its detector sees the same
    action and observation repeat (a nudge comes first for a repeating
    error); neither raises, so every runner asks."""
    status = convo.state.execution_status
    if status == ConversationExecutionStatus.STUCK:
        return "agent loop stuck repeating itself"
    if status == ConversationExecutionStatus.ERROR:
        return f"agent loop did not finish within {steps} steps"
    return None


def _render_prompt(path: Path, settings: AgentSettings) -> str:
    """Literal placeholder substitution — deliberately not str.format:
    format machinery turns any other brace in the prompt text into a
    crash (or a format-spec evaluation), and prompt files are edited as
    prose."""
    return path.read_text().replace("{target_repo}", settings.target_repo)


def _prompt(name: str, settings: AgentSettings, model: str) -> str:
    """The mode's system prompt: its procedure, then the SDK sections."""
    procedure = _render_prompt(PROMPTS_DIR / f"{name}.md", settings)
    return compose(procedure, PROMPT_SECTIONS[name], model)


def _clip(text: str, limit: int = _OUTPUT_CLIP, keep: str = "tail") -> str:
    if len(text) <= limit:
        return text
    marker = f"[... {len(text) - limit} chars clipped ...]\n"
    return text[:limit] + "\n" + marker if keep == "head" else marker + text[-limit:]


def _test_report(result: dict[str, Any]) -> str:
    return (
        f"Test run finished with exit code {result.get('exit_code')}.\n"
        f"stdout:\n{_clip(str(result.get('stdout', '')))}\n"
        f"stderr:\n{_clip(str(result.get('stderr', '')))}"
    )


@dataclass
class Workspace:
    """What the host learned about the sandbox before the model saw it:
    the root listing, and the repository's instructions file if it has
    one — read under the pod's own token, so a role that may not read
    simply gets no instructions."""

    entries: str
    instructions: str | None = None

    def context(self) -> AgentContext | None:
        """The instructions as a repo skill: the SDK renders it in the
        system message's dynamic block inside its UNTRUSTED_CONTENT
        framing, which is exactly what a file from the repository is."""
        if self.instructions is None:
            return None
        return AgentContext(
            skills=[Skill(name=REPO_INSTRUCTIONS_FILE, content=self.instructions)],
            current_datetime=None,
        )


def _survey(bridge: GatewayBridge, run_id: str) -> Workspace:
    listing = bridge.call("sandbox.list_dir", {"run_id": run_id, "path": "."})
    entries = ", ".join(
        f"{e['name']}{'/' if e['type'] == 'dir' else ''}" for e in listing["entries"]
    )
    has_instructions = any(
        e["name"] == REPO_INSTRUCTIONS_FILE and e["type"] == "file"
        for e in listing["entries"]
    )
    if not has_instructions:
        return Workspace(entries)
    try:
        read = bridge.call(
            "sandbox.read_file", {"run_id": run_id, "path": REPO_INSTRUCTIONS_FILE}
        )
    except ToolCallError as exc:
        log.info("repo instructions unread", extra={"reason": str(exc)})
        return Workspace(entries)
    return Workspace(
        entries, _clip(str(read["content"]), REPO_INSTRUCTIONS_CLIP, keep="head")
    )


def _act_prompt(task_text: str, entries: str) -> str:
    return (
        f"Task:\n{task_text}\n\n"
        f"Workspace root entries: {entries}\n\n"
        "Carry out this slice, then stop — the test suite is run for you "
        "between turns."
    )


def _plan_prompt(task_text: str, entries: str) -> str:
    return (
        f"Task:\n{task_text}\n\n"
        f"Workspace root entries: {entries}\n\n"
        "Write only the numbered brief."
    )


@dataclass
class Session:
    convo: Conversation
    bridge: GatewayBridge
    records: ToolRecords
    # None for a dispatch with no sandbox (the PM).
    workspace: Workspace | None = None


@contextmanager
def _session(
    settings: AgentSettings,
    run_id: str,
    prompt: str,
    tools: list[str],
    max_steps: int,
    sandboxed: bool = True,
) -> Iterator[Session]:
    """One dispatch's machinery: a gateway session under the pod's own
    token, a scratch workspace, and an SDK conversation on the mode's
    prompt and tool surface. A sandboxed mode surveys the run's sandbox
    first, so the conversation opens knowing the tree and the
    repository's instructions. The records capture every tool result;
    the conversation is closed on the way out, whatever happened inside."""
    token = settings.resolve_gateway_token()
    records = ToolRecords()
    llm = build_llm(settings, run_id, token)
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        workspace = _survey(bridge, run_id) if sandboxed else None
        ctx = HandsContext(bridge, settings, run_id, records)
        with tempfile.TemporaryDirectory(prefix="brain-") as scratch:
            convo = _build_conversation(
                llm,
                _prompt(prompt, settings, llm.model),
                tools,
                ctx,
                scratch,
                max_steps=max_steps,
                agent_context=workspace.context() if workspace else None,
            )
            try:
                yield Session(convo, bridge, records, workspace)
            finally:
                convo.close()


def _completion(llm: LLM, system_prompt: str, user_text: str) -> str:
    """One tool-less completion: the text the model answered with."""
    response = llm.completion(
        messages=[
            Message(role="system", content=[TextContent(text=system_prompt)]),
            Message(role="user", content=[TextContent(text=user_text)]),
        ]
    )
    return "".join(
        block.text for block in response.message.content if isinstance(block, TextContent)
    ).strip()


def implement_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """mode=implement: the SDK loop acts (read/write/list/exec, never
    open_pr), the host runs the ADVISORY verify between turns, and advisory
    green ends the dispatch with a completion claim. The gate of record is
    the platform's own `sandbox.run_tests` in the implement workflow."""
    with _session(settings, run_id, "implement", ACT_TOOLS, ACT_STEP_CAP) as s:
        convo = s.convo
        assert s.workspace is not None
        convo.send_message(_act_prompt(task_text, s.workspace.entries))
        iterations = 0
        while True:
            convo.run()
            if failure := _loop_failure(convo, ACT_STEP_CAP):
                return RunResult(
                    iterations=iterations,
                    error=f"{failure} (after {iterations} verify cycles)",
                )
            iterations += 1
            try:
                result = s.bridge.call("sandbox.run_tests", {"run_id": run_id})
            except ToolCallError as exc:
                result = {"exit_code": -1, "stdout": "", "stderr": str(exc)}
            passed = result.get("exit_code") == 0
            log.info("verify", extra={"iteration": iterations, "passed": passed})
            if passed:
                # Advisory green = the completion claim: the platform verify
                # is the gate of record, and the workflow directs delivery
                # in a separate dispatch.
                return RunResult(iterations=iterations)
            if iterations >= settings.max_iterations:
                return RunResult(
                    iterations=iterations,
                    error=f"tests still failing after {iterations} iterations",
                )
            convo.send_message(_test_report(result) + "\n\nFix the failures.")


def deliver_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """The workflow-directed delivery dispatch:
    only ever dispatched after the platform verify gate passed.
    Commit the sandbox's work on the task's branch, bundle it, open the PR.
    A policy denial comes back as the tool observation so the model can
    recover — deny-and-recover, not deny-and-halt."""
    # One SDK step is one delivery attempt (commit + open_pr); the injected
    # scenario must fit attempt-deny-recover.
    max_steps = max(settings.max_iterations, 4)
    with _session(settings, run_id, "deliver", DELIVER_TOOLS, max_steps) as s:
        convo, records = s.convo, s.records
        convo.send_message(
            f"Task:\n{task_text}\n\n"
            "The platform has verified your work in the sandbox: the "
            "test suite is green. Deliver it now — commit your changes "
            "on the task's feature/<slug> branch and open the pull "
            "request with the open_pr tool."
        )
        convo.run()
        failure = _loop_failure(convo, max_steps)
    if records.pr_url is None:
        return RunResult(
            error=failure or "agent stopped without opening a pull request",
            iterations=1,
        )
    log.info("delivered", extra={"pr_url": records.pr_url})
    return RunResult(pr_url=records.pr_url, iterations=1)


def review_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """The ephemeral reviewer's dispatch: read the
    diff via git.read, execute the suite in the run's sandbox (provisioned
    from the PR ref), post findings via git.comment, and end with the
    `verdict` tool — pass/fail plus a one-line summary, captured as a
    record like every other tool result. Read-and-judge
    only: no write_file, no open_pr, and the compiled sdet scope enforces
    the same at the gateway. A dispatch without posted findings or without
    a verdict is a failed review."""
    max_steps = settings.max_iterations * 2
    with _session(settings, run_id, "review", REVIEWER_TOOLS, max_steps) as s:
        convo, records = s.convo, s.records
        convo.send_message(f"Task:\n{task_text}")
        convo.run()
        failure = _loop_failure(convo, max_steps)
    if not records.comment_urls:
        return RunResult(error=failure or "reviewer posted no findings")
    if records.verdict is None:
        return RunResult(
            error=failure or "reviewer finished without a verdict",
            comment_urls=records.comment_urls,
        )
    log.info(
        "review finished",
        extra={"comments": records.comment_urls, "verdict": records.verdict},
    )
    return RunResult(
        comment_urls=records.comment_urls,
        verdict=records.verdict,
        verdict_summary=records.verdict_summary,
    )


def pm_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """The PM runtime: scope a goal, file tickets, record
    crew intent — through governed tools only. No sandbox, no delivery, no
    private memory: the backlog and the run history are the PM's only
    cross-run state. Executor-tool refusal is the
    gateway's job; a deny surfaces as a tool observation the model sees."""
    with _session(settings, run_id, "pm", PM_TOOLS, PM_STEP_CAP, sandboxed=False) as s:
        convo, records = s.convo, s.records
        convo.send_message(f"Goal:\n{task_text}")
        convo.run()
        failure = _loop_failure(convo, PM_STEP_CAP)
    if failure:
        return RunResult(
            error=failure,
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


# The decide contract: exactly one DECISION line and one RATIONALE line.
# The rationale is required — an unexplained decision is a failed dispatch,
# because the journal is half the point of asking.
DECISION_RE = re.compile(r"^DECISION:\s*(?:(implement|review)\s+#(\d+)|(idle))\s*$", re.M)
RATIONALE_RE = re.compile(r"^RATIONALE:\s*(\S.*)$", re.M)


def parse_decision(text: str) -> dict[str, Any] | None:
    """The two-line decide contract, or None if the reply breaks it."""
    decision = DECISION_RE.search(text)
    rationale = RATIONALE_RE.search(text)
    if decision is None or rationale is None:
        return None
    action, target, idle = decision.groups()
    return {
        "action": action or idle,
        "target": int(target) if target else None,
        "rationale": rationale.group(1).strip(),
    }


def decide_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """mode=decide: the conduct heartbeat's question. LLM-only — one
    completion, no tools, no gateway session: the observation in the task
    text is everything the PM may consider, and the platform validates the
    answer against it. The PM chooses; it starts nothing — actuation stays
    the conductor workflow's, under the compiled conduct.start decision."""
    llm = build_llm(settings, run_id, settings.resolve_gateway_token())
    text = _completion(llm, _prompt("decide", settings, llm.model), task_text)
    decision = parse_decision(text)
    if decision is None:
        return RunResult(
            error=f"decide reply broke the two-line contract: {text[:200]!r}"
        )
    log.info("decision", extra=decision)
    return RunResult(decision=decision)


def plan_dispatch(settings: AgentSettings, run_id: str, task_text: str) -> RunResult:
    """Planner mode: read the run's sandbox, write nothing, answer with
    the task brief. The loop offers only the read tools — the planner's
    compiled scope is read-only, and it calls the gateway under its own
    pod's token, so policy enforces the same. The brief is the agent's
    final text, whether it ended with a plain reply or `finish`."""
    with _session(settings, run_id, "plan", PLAN_TOOLS, PLAN_STEP_CAP) as s:
        convo = s.convo
        assert s.workspace is not None
        convo.send_message(_plan_prompt(task_text, s.workspace.entries))
        convo.run()
        if failure := _loop_failure(convo, PLAN_STEP_CAP):
            return RunResult(error=failure)
        brief = get_agent_final_response(convo.state.events).strip()
    if not brief:
        return RunResult(error="planner produced an empty brief")
    return RunResult(brief=brief)
