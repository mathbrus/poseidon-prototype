"""The procedures an agent host runs: the OpenHands Agent SDK as a
library behind Poseidon contracts.

The SDK owns the reasoning loop, context machinery and coding heuristics —
nothing else. Enforced boundaries:

- Every side effect is a Hands tool resolving to a gateway MCP call
  (`agent.hands`); the host holds the only credential and the SDK's default
  tools are control flow only (finish/think). The OpenHands built-ins and
  its sub-agent machinery (`TaskToolSet`) are never installed or registered.
- The host starts and stops no sandboxes and owns no process-level
  retries/timeouts/cancellation — the workflow dispatches into this host
  and waits; `num_retries=1` keeps the SDK from grinding on errors the
  workflow owns (a `budget_exhausted` 429 surfaces immediately).
- The implement procedure ends at a completion claim: its tool surface has
  no `open_pr`, its advisory verify (host-driven `sandbox.run_tests`
  between conversation turns) only shapes iteration, and delivery is the
  separate workflow-directed `deliver` procedure after the platform
  verify gate.

Every procedure is one synchronous function — the SDK conversation loop
is blocking — run in a worker thread under the async dispatch app
(`asyncio.to_thread`).
The `GatewayBridge` gives the tool executors a sync facade over the async MCP
client by parking the session on a dedicated event-loop thread.
"""

import asyncio
import re
import tempfile
import threading
from collections.abc import Callable, Iterator, Sequence
from concurrent.futures import Future
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any

from openhands.sdk import (
    LLM,
    Agent,
    Conversation,
    ConversationExecutionStatus,
    LLMSummarizingCondenser,
)
from openhands.sdk.agent.parallel_executor import ParallelToolExecutor
from openhands.sdk.context import AgentContext
from openhands.sdk.llm import Message, TextContent
from pydantic import BaseModel, SecretStr

from agent.config import AgentSettings
from agent.gateway_client import McpGateway, ToolCallError
from agent.hands import (
    DELIVER_TOOLS,
    IMPLEMENT_TOOLS,
    REVIEW_TOOLS,
    SCOPE_TOOLS,
    WORKSPACE_TOOLS,
    HandsContext,
    ToolRecords,
    hands,
)
from agent.prompting import REPO_INSTRUCTIONS_FILE, system_prompt, untrusted_context
from common.telemetry import get_logger
from common.text import clip_tail
from common.topology import RUN_ID_HEADER

# Step caps per procedure — engineering limits on how long one SDK loop
# may run; money is bounded by the per-run cap either way.
# Scope is a handful of tool calls; the cap is a backstop.
SCOPE_STEP_CAP = 10
# Steps allowed within one implement cycle before tests are forced to run,
# and how many advisory verify cycles an implement dispatch gets before it
# reports the tests still failing. The platform's own verify gate and the
# blueprint's `max_verify_retries` bound the run; these bound one dispatch.
IMPLEMENT_STEP_CAP = 20
VERIFY_CYCLES = 10
# One SDK step is one delivery attempt (commit + open_pr); a denied
# attempt and its recovery must fit.
DELIVER_STEP_CAP = 4
# Read the diff, run the suite, post findings, judge.
REVIEW_STEP_CAP = 30
# Condensation is real context machinery for a real model; the sizes keep it
# far out of reach of the short deterministic e2e conversations.
CONDENSER_MAX_SIZE = 120
CONDENSER_KEEP_FIRST = 4
# Tool calls from one reply that may run at once — only ever reads.
TOOL_CONCURRENCY = 4
_OUTPUT_CLIP = 4000

log = get_logger("agent.procedures")


class ProcedureResult(BaseModel):
    """What a procedure reports back: only fields captured from tool
    results or parsed from a typed reply, never the model's prose."""

    pr_url: str | None = None
    iterations: int = 0
    error: str | None = None
    # Scope: the tickets it filed — what the run record links to.
    ticket_urls: list[str] = []
    # The comments a dispatch posted: the reviewer's findings on the PR,
    # or the PM's summary on the ticket(s) it filed.
    comment_urls: list[str] = []
    # Review: the structured verdict — pass/fail plus the reviewer's
    # rationale. The review workflow refuses to reach the merge gate
    # without a verdict; what the verdict releases is compiled policy's
    # decision, never the reviewer's.
    verdict: str | None = None
    verdict_rationale: str | None = None
    # Decide: {action, target, rationale} — the heartbeat's
    # chosen next action. The PM proposes; the platform validates,
    # authorizes and starts.
    decision: dict[str, Any] | None = None


class GatewayBridge:
    """Sync facade over the async MCP gateway session for the SDK's
    synchronous tool executors. The session lives on its own event-loop
    thread, opened and closed inside ONE long-lived task there — the MCP
    client's anyio cancel scopes are task-bound, so entering and exiting
    the session from separately scheduled coroutines corrupts them.
    `call` blocks its calling thread on a per-call coroutine; several tool
    threads may be in `call` at once (the SDK fans a batch of reads out),
    and the MCP session multiplexes them — each call's cancel scope opens
    and closes inside its own coroutine."""

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
    """The LLM gateway client, SDK-shaped: authenticated with the workload
    credential, every call attributed to its run via the header. The model
    name is the deployment's `MODEL`, the same one the gateway pins on every
    call; here it also selects the SDK's wire shape (Responses vs chat) and
    its prompt guidance. litellm needs a provider prefix to route to the
    custom base_url; the wire model name stays the bare setting."""
    model = settings.model if "/" in settings.model else f"openai/{settings.model}"
    return LLM(
        model=model,
        base_url=settings.llm_proxy_url,
        api_key=SecretStr(token),
        extra_headers={RUN_ID_HEADER: run_id},
        num_retries=1,  # the workflow owns retries; budget 429s surface now
        usage_id=run_id,
    )


class OrderedReadsExecutor(ParallelToolExecutor):
    """The SDK's batch executor with one rule on top: a batch fans out
    only when every call in it is read-only; a batch with any write,
    command or delivery in it runs one call at a time, in the order the
    model gave. The stock executor hands a whole batch to a thread pool,
    where the order in which writes acquire their locks is the
    scheduler's — and a commit must not land before the checkout. A
    one-call batch is the SDK's own in-order path, so a mixed batch is
    simply handed over one call at a time."""

    @staticmethod
    def _all_read_only(actions: Sequence[Any], tools: dict[str, Any] | None) -> bool:
        if not tools:
            return False
        for action in actions:
            tool = tools.get(action.tool_name)
            if tool is None or tool.annotations is None:
                return False
            if not tool.annotations.readOnlyHint:
                return False
        return True

    def execute_batch(
        self,
        action_events: Sequence[Any],
        tool_runner: Callable[[Any], list[Any]],
        tools: dict[str, Any] | None = None,
        cancel_token: Any = None,
        span_owner: object | None = None,
    ) -> list[list[Any]]:
        if self._all_read_only(action_events, tools):
            return super().execute_batch(
                action_events, tool_runner, tools, cancel_token, span_owner
            )
        return [
            result
            for action in action_events
            for result in super().execute_batch(
                [action], tool_runner, tools, cancel_token, span_owner
            )
        ]

    async def aexecute_batch(
        self,
        action_events: Sequence[Any],
        tool_runner: Callable[[Any], list[Any]],
        tools: dict[str, Any] | None = None,
        cancel_token: Any = None,
        span_owner: object | None = None,
    ) -> list[list[Any]]:
        if self._all_read_only(action_events, tools):
            return await super().aexecute_batch(
                action_events, tool_runner, tools, cancel_token, span_owner
            )
        results = []
        for action in action_events:
            results += await super().aexecute_batch(
                [action], tool_runner, tools, cancel_token, span_owner
            )
        return results


class PoseidonAgent(Agent):
    """The SDK agent with its batch executor swapped for the ordered
    one. Installed in the SDK's own construction hook so any path that
    rebuilds the agent rebuilds the guarantee with it."""

    def model_post_init(self, context: Any) -> None:
        super().model_post_init(context)
        self._parallel_executor = OrderedReadsExecutor(
            max_workers=self.tool_concurrency_limit
        )


def _build_conversation(
    llm: LLM,
    prompt: str,
    tool_names: list[str],
    ctx: HandsContext,
    workspace: str,
    max_steps: int,
    agent_context: AgentContext | None,
) -> Conversation:
    agent = PoseidonAgent(
        llm=llm,
        tools=hands(tool_names),
        system_prompt=prompt,
        agent_context=agent_context,
        condenser=LLMSummarizingCondenser(
            llm=llm, max_size=CONDENSER_MAX_SIZE, keep_first=CONDENSER_KEEP_FIRST
        ),
        tool_concurrency_limit=TOOL_CONCURRENCY,
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


def _loop_failure(convo: Conversation) -> str | None:
    """Why the SDK loop stopped short, or None if it finished. The SDK
    sets ERROR at the step cap and STUCK when its detector sees the same
    action and observation repeat (a nudge comes first for a repeating
    error); neither raises, so every procedure asks."""
    status = convo.state.execution_status
    if status == ConversationExecutionStatus.STUCK:
        return "agent loop stuck repeating itself"
    if status == ConversationExecutionStatus.ERROR:
        return f"agent loop did not finish within {convo.state.max_iterations} steps"
    return None


def _test_report(result: dict[str, Any]) -> str:
    return (
        f"Test run finished with exit code {result.get('exit_code')}.\n"
        f"stdout:\n{clip_tail(str(result.get('stdout', '')), _OUTPUT_CLIP)}\n"
        f"stderr:\n{clip_tail(str(result.get('stderr', '')), _OUTPUT_CLIP)}"
    )


@dataclass
class Workspace:
    """What the host learned about the sandbox before the model saw it:
    the root listing, and the repository's instructions file if it has
    one — read under the pod's own token, so a role that may not read
    simply gets no instructions. Empty for a dispatch with no sandbox."""

    entries: str = ""
    instructions: str | None = None


def _survey(bridge: GatewayBridge, run_id: str) -> Workspace:
    listing = bridge.call("sandbox.list_dir", {"run_id": run_id, "path": "."})
    names = {e["name"]: e["type"] for e in listing["entries"]}
    entries = ", ".join(f"{n}{'/' if t == 'dir' else ''}" for n, t in names.items())
    if names.get(REPO_INSTRUCTIONS_FILE) != "file":
        return Workspace(entries)
    try:
        read = bridge.call(
            "sandbox.read_file", {"run_id": run_id, "path": REPO_INSTRUCTIONS_FILE}
        )
    except ToolCallError as exc:
        log.info("repo instructions unread", extra={"reason": str(exc)})
        return Workspace(entries)
    return Workspace(entries, str(read["content"]))


def _act_prompt(task_text: str, entries: str) -> str:
    return (
        f"Task:\n{task_text}\n\n"
        f"Workspace root entries: {entries}\n\n"
        "Carry out this slice, then stop — the test suite is run for you "
        "between turns."
    )


@dataclass
class Session:
    convo: Conversation
    bridge: GatewayBridge
    records: ToolRecords
    workspace: Workspace


@contextmanager
def _session(
    settings: AgentSettings,
    run_id: str,
    procedure: str,
    tools: list[str],
    max_steps: int,
    documents: dict[str, str] | None = None,
) -> Iterator[Session]:
    """One dispatch's machinery: a gateway session under the pod's own
    token, a scratch workspace, and an SDK conversation on the procedure's
    prompt and tool surface. A procedure that holds sandbox tools surveys the
    run's sandbox first, so the conversation opens knowing the tree and
    the repository's instructions; the company's documents the dispatch
    carries join them as untrusted context. The records capture every tool
    result; the conversation is closed on the way out, whatever happened
    inside."""
    token = settings.resolve_gateway_token()
    records = ToolRecords()
    llm = build_llm(settings, run_id, token)
    with GatewayBridge(settings.mcp_gateway_url, token) as bridge:
        workspace = (
            _survey(bridge, run_id) if WORKSPACE_TOOLS & set(tools) else Workspace()
        )
        ctx = HandsContext(bridge, settings, run_id, records)
        with tempfile.TemporaryDirectory(prefix="brain-") as scratch:
            convo = _build_conversation(
                llm,
                system_prompt(procedure, settings, llm.model),
                tools,
                ctx,
                scratch,
                max_steps=max_steps,
                agent_context=untrusted_context(workspace.instructions, documents or {}),
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


def run_implement(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    documents: dict[str, str] | None = None,
) -> ProcedureResult:
    """implement: the SDK loop acts (read/write/list/exec, never open_pr),
    the host runs the ADVISORY verify between turns, and advisory green
    ends the dispatch with a completion claim. The gate of record is the
    platform's own `sandbox.run_tests` in the implement workflow."""
    with _session(
        settings, run_id, "implement", IMPLEMENT_TOOLS, IMPLEMENT_STEP_CAP, documents
    ) as s:
        convo, records = s.convo, s.records
        convo.send_message(_act_prompt(task_text, s.workspace.entries))
        iterations = 0
        while True:
            convo.run()
            if failure := _loop_failure(convo):
                return ProcedureResult(
                    iterations=iterations,
                    error=f"{failure} (after {iterations} verify cycles)",
                    ticket_urls=records.ticket_urls,
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
                return ProcedureResult(
                    iterations=iterations, ticket_urls=records.ticket_urls
                )
            if iterations >= VERIFY_CYCLES:
                return ProcedureResult(
                    iterations=iterations,
                    error=f"tests still failing after {iterations} iterations",
                    ticket_urls=records.ticket_urls,
                )
            convo.send_message(_test_report(result) + "\n\nFix the failures.")


def run_deliver(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    documents: dict[str, str] | None = None,
) -> ProcedureResult:
    """deliver: the workflow-directed delivery, only ever dispatched after
    the platform verify gate passed. Commit the sandbox's work on the
    task's branch, bundle it, open the PR. A policy denial comes back as
    the tool observation so the model can recover — deny-and-recover, not
    deny-and-halt."""
    with _session(settings, run_id, "deliver", DELIVER_TOOLS, DELIVER_STEP_CAP) as s:
        convo, records = s.convo, s.records
        convo.send_message(
            f"Task:\n{task_text}\n\n"
            "The platform has verified your work in the sandbox: the "
            "test suite is green. Deliver it now — commit your changes "
            "on the task's feature/<slug> branch and open the pull "
            "request with the open_pr tool."
        )
        convo.run()
        failure = _loop_failure(convo)
    if records.pr_url is None:
        return ProcedureResult(
            error=failure or "agent stopped without opening a pull request",
            iterations=1,
        )
    log.info("delivered", extra={"pr_url": records.pr_url})
    return ProcedureResult(pr_url=records.pr_url, iterations=1)


def run_review(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    documents: dict[str, str] | None = None,
) -> ProcedureResult:
    """review: the ephemeral reviewer's dispatch: read the
    diff via git.read, execute the suite in the run's sandbox (provisioned
    from the PR ref), post findings via git.comment, and end with the
    `verdict` tool — pass/fail plus a one-line summary, captured as a
    record like every other tool result. Read-and-judge
    only: no write_file, no open_pr, and the compiled reviewer scope enforces
    the same at the gateway. A dispatch without posted findings or without
    a verdict is a failed review. Whatever the exit, the tickets the
    reviewer had the platform file (an architecture request) are reported:
    they are this run's, and the record must carry them."""
    with _session(
        settings, run_id, "review", REVIEW_TOOLS, REVIEW_STEP_CAP, documents
    ) as s:
        convo, records = s.convo, s.records
        convo.send_message(f"Task:\n{task_text}")
        convo.run()
        failure = _loop_failure(convo)
    partial = dict(ticket_urls=records.ticket_urls, comment_urls=records.comment_urls)
    if not records.comment_urls:
        return ProcedureResult(error=failure or "reviewer posted no findings", **partial)
    if records.verdict is None:
        return ProcedureResult(
            error=failure or "reviewer finished without a verdict", **partial
        )
    log.info(
        "review finished",
        extra={"comments": records.comment_urls, "verdict": records.verdict},
    )
    return ProcedureResult(
        verdict=records.verdict, verdict_rationale=records.verdict_rationale, **partial
    )


def run_scope(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    documents: dict[str, str] | None = None,
) -> ProcedureResult:
    """scope: the PM reads the observation — the company's documents and
    the world as the heartbeat sees it — and files the next slice of work
    as tickets, leaving a one-paragraph summary as a comment on each; all
    through governed tools. No sandbox, no delivery, no private memory:
    the backlog is the PM's cross-run state, and the summary comment is
    what a later reader of that backlog finds. Filing nothing is a valid
    outcome (the strategy may be delivered); a dispatch that filed tickets
    but posted no summary is a failed dispatch — the tickets are still
    reported, so they stay linked to the run. Refusing a tool call is the
    gateway's job; a deny surfaces as a tool observation the model sees."""
    with _session(settings, run_id, "scope", SCOPE_TOOLS, SCOPE_STEP_CAP) as s:
        convo, records = s.convo, s.records
        convo.send_message(task_text)
        convo.run()
        failure = _loop_failure(convo)
    partial = dict(ticket_urls=records.ticket_urls, comment_urls=records.comment_urls)
    if failure:
        return ProcedureResult(error=failure, **partial)
    if records.ticket_urls and not records.comment_urls:
        return ProcedureResult(
            error="PM filed tickets without posting its summary", **partial
        )
    log.info(
        "pm finished",
        extra={"tickets": records.ticket_urls, "comments": records.comment_urls},
    )
    return ProcedureResult(**partial)


# The decide contract: exactly one DECISION line and one RATIONALE line.
# The rationale is required — an unexplained decision is a failed dispatch,
# because the journal is half the point of asking.
DECISION_RE = re.compile(
    r"^DECISION:\s*(?:(implement|review)\s+#(\d+)|(scope|idle))\s*$", re.M
)
RATIONALE_RE = re.compile(r"^RATIONALE:\s*(\S.*)$", re.M)


def parse_decision(text: str) -> dict[str, Any] | None:
    """The two-line decide contract, or None if the reply breaks it."""
    decision = DECISION_RE.search(text)
    rationale = RATIONALE_RE.search(text)
    if decision is None or rationale is None:
        return None
    action, target, bare = decision.groups()
    return {
        "action": action or bare,
        "target": int(target) if target else None,
        "rationale": rationale.group(1).strip(),
    }


def run_decide(
    settings: AgentSettings,
    run_id: str,
    task_text: str,
    documents: dict[str, str] | None = None,
) -> ProcedureResult:
    """decide: the heartbeat's question. LLM-only — one
    completion, no tools, no gateway session: the observation in the task
    text is everything the PM may consider, and the platform validates the
    answer against it. The PM chooses; it starts nothing — actuation stays
    the heartbeat's, under the compiled heartbeat.start decision."""
    llm = build_llm(settings, run_id, settings.resolve_gateway_token())
    text = _completion(llm, system_prompt("decide", settings, llm.model), task_text)
    decision = parse_decision(text)
    if decision is None:
        return ProcedureResult(
            error=f"decide reply broke the two-line contract: {text[:200]!r}"
        )
    log.info("decision", extra=decision)
    return ProcedureResult(decision=decision)
