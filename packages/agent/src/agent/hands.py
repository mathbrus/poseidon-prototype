"""The Hands adapter (§4.3, plan step 22): OpenHands tool definitions that
resolve every side effect to a gateway MCP call.

Two PoC-toolbox mechanisms carry over verbatim:

- `run_id` (and `target_repo`) are injected at call time and appear in no
  action schema — the model cannot see or forge them.
- `open_pr` stays a composite: `sandbox.git_bundle` then `git.open_pr`, so
  base64 bundles never enter the model context.

A gateway denial (`ToolCallError`, carrying the OPA rule name) becomes the
tool observation text (`ERROR: ...`) — denial-as-tool-result, the E2
recovery path. The brain's only effectors are these tools: the OpenHands
built-ins (bash, file editor, browser) are not installed, and the SDK's own
defaults are control flow only (finish/think).

Tool resolution is lazy — the SDK resolves specs on a worker thread at
first use — so the per-dispatch context (gateway bridge, run identity,
result records) is a module-level current-context, not a contextvar:
`HandsContext.activate()` binds it. One dispatch at a time per brain host
is the C6 contract (409 on re-dispatch), so there is never a second
context to race with.
"""

import json
from dataclasses import dataclass, field
from typing import Any, Protocol

from pydantic import Field

from openhands.sdk import TextContent, register_tool
from openhands.sdk.tool import ToolDefinition, ToolExecutor
from openhands.sdk.tool.schema import Action, Observation
from openhands.sdk.tool.spec import Tool

from agent.config import AgentSettings
from agent.gateway_client import ToolCallError

# Per-phase tool surfaces — the structural guarantee: the implement agent
# has no `open_pr` (delivery before the platform verify gate is impossible
# by construction); the reviewer writes nothing but its findings comment.
ACT_TOOLS = ["list_dir", "read_file", "write_file", "exec"]
DELIVER_TOOLS = ACT_TOOLS + ["open_pr"]
PM_TOOLS = ["create_issue", "read_issues", "request_crew"]
REVIEWER_TOOLS = ["list_dir", "read_file", "exec", "run_tests", "read_pr", "comment_pr"]


class GatewayCaller(Protocol):
    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ToolRecords:
    """What the dispatch reports back on the C6 contract, captured as the
    executors see results — no parsing of the conversation afterwards."""

    pr_url: str | None = None
    ticket_urls: list[str] = field(default_factory=list)
    crew_request_id: str | None = None
    comment_urls: list[str] = field(default_factory=list)


@dataclass
class HandsContext:
    bridge: GatewayCaller
    settings: AgentSettings
    run_id: str
    records: ToolRecords

    def activate(self) -> None:
        global _CURRENT
        _CURRENT = self


_CURRENT: HandsContext | None = None


def current_context() -> HandsContext:
    if _CURRENT is None:
        raise RuntimeError(
            "no HandsContext active — a dispatch must activate() its "
            "context before the conversation resolves its tools"
        )
    return _CURRENT


# --- action schemas (the model-facing contract, run_id-free) -----------------


class ListDirAction(Action):
    path: str = Field(description="Directory path, relative to the workspace root.")


class ReadFileAction(Action):
    path: str = Field(description="File path, relative to the workspace root.")


class WriteFileAction(Action):
    path: str = Field(description="File path, relative to the workspace root.")
    content: str = Field(description="Full new file content.")


class ExecAction(Action):
    cmd: str = Field(description="Shell command line.")
    timeout_s: float = Field(default=60, description="Timeout in seconds (default 60).")


class RunTestsAction(Action):
    pass


class OpenPrAction(Action):
    branch: str = Field(description="Branch to deliver, e.g. feature/add-parser.")
    title: str = Field(description="Pull request title.")
    body: str = Field(description="Pull request description.")


class CreateIssueAction(Action):
    title: str = Field(description="Short, specific ticket title.")
    body: str = Field(description="The scoped work: context, acceptance criteria.")


class ReadIssuesAction(Action):
    number: int | None = Field(
        default=None, description="Optional issue number; omit for the open backlog."
    )


class RequestCrewAction(Action):
    crew_template: str = Field(description="Crew template name, e.g. feature-delivery.")
    reason: str = Field(description="Why, referencing the ticket(s).")


class ReadPrAction(Action):
    number: int = Field(description="Pull request number.")


class CommentPrAction(Action):
    number: int = Field(description="Pull request number.")
    body: str = Field(description="The findings: what you checked, what you found.")


class GatewayObservation(Observation):
    output: str = ""

    @property
    def to_llm_content(self) -> list[TextContent]:
        return [TextContent(text=self.output)]


# --- gateway mappings (run_id / target_repo injection lives here) ------------


def _list_dir(ctx: HandsContext, a: ListDirAction) -> dict[str, Any]:
    return ctx.bridge.call("sandbox.list_dir", {"run_id": ctx.run_id, "path": a.path})


def _read_file(ctx: HandsContext, a: ReadFileAction) -> dict[str, Any]:
    return ctx.bridge.call("sandbox.read_file", {"run_id": ctx.run_id, "path": a.path})


def _write_file(ctx: HandsContext, a: WriteFileAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "sandbox.write_file",
        {"run_id": ctx.run_id, "path": a.path, "content": a.content},
    )


def _exec(ctx: HandsContext, a: ExecAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "sandbox.exec",
        {"run_id": ctx.run_id, "cmd": a.cmd, "timeout_s": a.timeout_s},
    )


def _run_tests(ctx: HandsContext, a: RunTestsAction) -> dict[str, Any]:
    return ctx.bridge.call("sandbox.run_tests", {"run_id": ctx.run_id})


def _open_pr(ctx: HandsContext, a: OpenPrAction) -> dict[str, Any]:
    bundle = ctx.bridge.call(
        "sandbox.git_bundle", {"run_id": ctx.run_id, "branch": a.branch}
    )
    result = ctx.bridge.call(
        "git.open_pr",
        {
            "run_id": ctx.run_id,
            "repo": ctx.settings.target_repo,
            "branch": a.branch,
            "title": a.title,
            "body": a.body,
            "bundle_b64": bundle["bundle_b64"],
        },
    )
    if result.get("pr_url"):
        ctx.records.pr_url = result["pr_url"]
    # The bundle never enters the model context; the observation is the PR.
    return {"pr_url": result.get("pr_url")}


def _create_issue(ctx: HandsContext, a: CreateIssueAction) -> dict[str, Any]:
    result = ctx.bridge.call(
        "issues.create",
        {"run_id": ctx.run_id, "repo": ctx.settings.target_repo,
         "title": a.title, "body": a.body},
    )
    if result.get("url"):
        ctx.records.ticket_urls.append(result["url"])
    return result


def _read_issues(ctx: HandsContext, a: ReadIssuesAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "issues.read",
        {"run_id": ctx.run_id, "repo": ctx.settings.target_repo, "number": a.number},
    )


def _request_crew(ctx: HandsContext, a: RequestCrewAction) -> dict[str, Any]:
    result = ctx.bridge.call(
        "crew.request",
        {"run_id": ctx.run_id, "crew_template": a.crew_template, "reason": a.reason},
    )
    if result.get("id"):
        ctx.records.crew_request_id = result["id"]
    return result


def _read_pr(ctx: HandsContext, a: ReadPrAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "git.read",
        {"run_id": ctx.run_id, "repo": ctx.settings.target_repo,
         "number": int(a.number)},
    )


def _comment_pr(ctx: HandsContext, a: CommentPrAction) -> dict[str, Any]:
    result = ctx.bridge.call(
        "git.comment",
        {"run_id": ctx.run_id, "repo": ctx.settings.target_repo,
         "number": int(a.number), "body": a.body},
    )
    if result.get("url"):
        ctx.records.comment_urls.append(result["url"])
    return result


_TOOLS: dict[str, tuple[str, type[Action], Any]] = {
    "list_dir": (
        "List entries of a directory inside the workspace.",
        ListDirAction, _list_dir,
    ),
    "read_file": (
        "Read a text file from the workspace.",
        ReadFileAction, _read_file,
    ),
    "write_file": (
        "Write (create or overwrite) a text file in the workspace.",
        WriteFileAction, _write_file,
    ),
    "exec": (
        "Run a shell command in the workspace root (e.g. git, ls, python).",
        ExecAction, _exec,
    ),
    "run_tests": (
        "Run the repository's test suite in your sandbox.",
        RunTestsAction, _run_tests,
    ),
    "open_pr": (
        "Deliver the finished work: bundle the given branch and open a pull "
        "request against the target repository. Commit your changes on the "
        "branch first (via exec).",
        OpenPrAction, _open_pr,
    ),
    "create_issue": (
        "File a ticket on the target repository's issue tracker. Use one "
        "ticket per well-scoped piece of work.",
        CreateIssueAction, _create_issue,
    ),
    "read_issues": (
        "Read the open ticket backlog (or one ticket by number).",
        ReadIssuesAction, _read_issues,
    ),
    "request_crew": (
        "Request an ephemeral crew to carry out scoped work. This records "
        "intent for the platform to fulfill — it does not start anything "
        "by itself.",
        RequestCrewAction, _request_crew,
    ),
    "read_pr": (
        "Read a pull request: metadata plus the changed files with their "
        "patches.",
        ReadPrAction, _read_pr,
    ),
    "comment_pr": (
        "Post a review comment on the pull request — your findings, in one "
        "comment.",
        CommentPrAction, _comment_pr,
    ),
}


class _GatewayExecutor(ToolExecutor):
    def __init__(self, ctx: HandsContext, fn: Any) -> None:
        self._ctx = ctx
        self._fn = fn

    def __call__(self, action: Action, conversation: Any = None) -> GatewayObservation:
        try:
            result = self._fn(self._ctx, action)
        except ToolCallError as exc:
            # Policy denials and malformed calls feed back as the tool
            # result — the model sees the rule name and recovers (E2).
            return GatewayObservation(output=f"ERROR: {exc}")
        return GatewayObservation(output=json.dumps(result))


def _definition_class_name(tool_name: str) -> str:
    # "open_pr" -> "OpenPrTool"; ToolDefinition.__init_subclass__ derives
    # the model-facing tool name back from the class name (a ClassVar — a
    # `name=` constructor argument is ignored), so the class name IS the
    # tool-name contract.
    return "".join(part.capitalize() for part in tool_name.split("_")) + "Tool"


def _unused_create(cls: type, conv_state: Any = None, **params: Any) -> list:
    # Instances come from PoseidonHands.create; the per-name classes only
    # exist to carry the model-facing tool name. Satisfies the ABC.
    raise NotImplementedError(f"{cls.__name__} is built by PoseidonHands")


_DEFINITION_CLASSES: dict[str, type[ToolDefinition]] = {
    tool_name: type(
        _definition_class_name(tool_name),
        (ToolDefinition,),
        {"create": classmethod(_unused_create)},
    )
    for tool_name in _TOOLS
}
assert all(cls.name == tool_name for tool_name, cls in _DEFINITION_CLASSES.items())


class PoseidonHands(ToolDefinition):
    """Registered factory: `Tool(name="PoseidonHands", params={"names":
    [...]})` resolves to the named gateway tools, bound to the dispatch's
    `HandsContext`."""

    @classmethod
    def create(cls, conv_state: Any = None, **params: Any) -> list[ToolDefinition]:
        ctx = current_context()
        tools = []
        for name in params["names"]:
            description, action_type, fn = _TOOLS[name]
            tools.append(
                _DEFINITION_CLASSES[name](
                    description=description,
                    action_type=action_type,
                    observation_type=GatewayObservation,
                    executor=_GatewayExecutor(ctx, fn),
                )
            )
        return tools


register_tool("PoseidonHands", PoseidonHands)


def hands(names: list[str]) -> list[Tool]:
    """The Agent `tools` entry for one dispatch phase."""
    return [Tool(name="PoseidonHands", params={"names": names})]
