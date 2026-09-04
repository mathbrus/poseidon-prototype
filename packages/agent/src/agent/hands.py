"""The Hands adapter: OpenHands tool definitions that
resolve every side effect to a gateway MCP call.

Two mechanisms worth stating:

- `run_id` (and `target_repo`) are injected at call time and appear in no
  action schema — the model cannot see or forge them.
- `open_pr` stays a composite: `sandbox.git_bundle` then `git.open_pr`, so
  base64 bundles never enter the model context.

A gateway denial (`ToolCallError`, carrying the OPA rule name) becomes the
tool observation text (`ERROR: ...`) — denial-as-tool-result, the
deny-and-recover path. The brain's only effectors are these tools: the OpenHands
built-ins (bash, file editor, browser) are not installed, and the SDK's own
defaults are control flow only (finish/think).

Tool resolution is lazy — the SDK resolves specs on a worker thread at
first use — so the per-dispatch context (gateway bridge, run identity,
result records) is a module-level current-context, not a contextvar:
`HandsContext.activate()` binds it. One dispatch at a time per agent host
is the dispatch API's contract (409 on re-dispatch), so there is never a
second context to race with.
"""

import json
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any, Literal, Protocol

from openhands.sdk import TextContent, register_tool
from openhands.sdk.tool import ToolAnnotations, ToolDefinition, ToolExecutor
from openhands.sdk.tool.schema import Action, Observation
from openhands.sdk.tool.spec import Tool
from openhands.sdk.tool.tool import DeclaredResources
from pydantic import Field

from agent.config import AgentSettings
from agent.gateway_client import ToolCallError
from common.text import numbered_lines

# Per-procedure tool surfaces — the structural guarantee: implement has
# no `open_pr` (delivery before the platform verify gate is impossible by
# construction); review writes nothing but its findings comment.
IMPLEMENT_TOOLS = ["list_dir", "read_file", "write_file", "edit_file", "exec"]
DELIVER_TOOLS = IMPLEMENT_TOOLS + ["open_pr"]
SCOPE_TOOLS = ["create_issue", "read_issues", "comment_issue"]
REVIEW_TOOLS = [
    "list_dir",
    "read_file",
    "exec",
    "run_tests",
    "read_pr",
    "comment_pr",
    "verdict",
]
# The tools that address the run's sandbox: a procedure holding any of
# them gets the host's workspace survey before its first turn.
WORKSPACE_TOOLS = frozenset(
    {"list_dir", "read_file", "write_file", "edit_file", "exec", "run_tests"}
)


class GatewayCaller(Protocol):
    def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


@dataclass
class ToolRecords:
    """What the dispatch reports back over the dispatch API, captured as the
    tool executors see results — no parsing of the conversation afterwards."""

    pr_url: str | None = None
    ticket_urls: list[str] = field(default_factory=list)
    # The reviewer's findings on the PR, or the PM's summary on its tickets.
    comment_urls: list[str] = field(default_factory=list)
    # Review: the structured verdict. The reviewer *proposes*
    # pass/fail; what a verdict releases is the platform's decision, made
    # against compiled policy — never this record's.
    verdict: str | None = None
    verdict_summary: str | None = None


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
    # A list, not a pair: model-facing schemas stay within what every
    # provider accepts; the gateway's own schema is the pair.
    view_range: list[int] | None = Field(
        default=None,
        description="Optional [start, end] line range, 1-based and inclusive "
        "(end -1 = end of file). Omit to read the whole file.",
    )


class WriteFileAction(Action):
    path: str = Field(description="File path, relative to the workspace root.")
    content: str = Field(description="Full new file content.")


class EditFileAction(Action):
    command: Literal["str_replace", "insert"] = Field(
        description="str_replace: swap one exact occurrence of old_str for "
        "new_str. insert: add new_str after line insert_line."
    )
    path: str = Field(description="File path, relative to the workspace root.")
    old_str: str | None = Field(
        default=None,
        description="str_replace only: the exact text to replace; it must "
        "occur exactly once, so include enough surrounding lines.",
    )
    new_str: str = Field(description="The replacement text, or the text to insert.")
    insert_line: int | None = Field(
        default=None,
        description="insert only: the line number to insert after (0 = top).",
    )


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


class ReadPrAction(Action):
    number: int = Field(description="Pull request number.")


class CommentPrAction(Action):
    number: int = Field(description="Pull request number.")
    body: str = Field(description="The findings: what you checked, what you found.")


class CommentIssueAction(Action):
    number: int = Field(description="Ticket number, as create_issue's reply named it.")
    body: str = Field(
        description="One paragraph: what you set up for this ticket and why, "
        "written for whoever picks it up."
    )


class VerdictAction(Action):
    verdict: Literal["pass", "fail"] = Field(
        description="Your judgement of the change: pass or fail."
    )
    summary: str = Field(description="One line grounding the verdict in your findings.")


class GatewayObservation(Observation):
    output: str = ""

    @property
    def to_llm_content(self) -> list[TextContent]:
        return [TextContent(text=self.output)]


# --- gateway mappings (run_id / target_repo injection lives here) ------------


def _list_dir(ctx: HandsContext, a: ListDirAction) -> dict[str, Any]:
    return ctx.bridge.call("sandbox.list_dir", {"run_id": ctx.run_id, "path": a.path})


def _read_file(ctx: HandsContext, a: ReadFileAction) -> dict[str, Any]:
    args: dict[str, Any] = {"run_id": ctx.run_id, "path": a.path}
    if a.view_range is not None:
        args["view_range"] = a.view_range
    return ctx.bridge.call("sandbox.read_file", args)


def _render_read(a: ReadFileAction, result: dict[str, Any]) -> str:
    """Numbered lines — what str_replace and insert anchor on — under a
    header saying which lines of how many these are."""
    lines = str(result["content"]).splitlines(keepends=True)
    first = int(result.get("start_line", 1))
    total = int(result.get("total_lines", len(lines)))
    last = first + len(lines) - 1 if lines else first
    return f"{a.path}: lines {first}-{last} of {total}\n{numbered_lines(lines, first)}"


def _write_file(ctx: HandsContext, a: WriteFileAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "sandbox.write_file",
        {"run_id": ctx.run_id, "path": a.path, "content": a.content},
    )


def _edit_file(ctx: HandsContext, a: EditFileAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "sandbox.edit_file",
        {
            "run_id": ctx.run_id,
            "path": a.path,
            "command": a.command,
            "old_str": a.old_str,
            "new_str": a.new_str,
            "insert_line": a.insert_line,
        },
    )


def _render_edit(a: EditFileAction, result: dict[str, Any]) -> str:
    return f"Edited {a.path}. The lines around the change now read:\n{result['snippet']}"


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
        {
            "run_id": ctx.run_id,
            "repo": ctx.settings.target_repo,
            "title": a.title,
            "body": a.body,
        },
    )
    if result.get("url"):
        ctx.records.ticket_urls.append(result["url"])
    return result


def _read_issues(ctx: HandsContext, a: ReadIssuesAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "issues.read",
        {"run_id": ctx.run_id, "repo": ctx.settings.target_repo, "number": a.number},
    )


def _read_pr(ctx: HandsContext, a: ReadPrAction) -> dict[str, Any]:
    return ctx.bridge.call(
        "git.read",
        {"run_id": ctx.run_id, "repo": ctx.settings.target_repo, "number": int(a.number)},
    )


def _verdict(ctx: HandsContext, a: VerdictAction) -> dict[str, Any]:
    """The one local tool: a verdict is the dispatch's return value, not a
    side effect, so there is nothing for the gateway to mediate. It is
    captured here like every other record — never parsed out of prose."""
    ctx.records.verdict = a.verdict
    ctx.records.verdict_summary = a.summary
    return {"recorded": a.verdict}


def _comment_via(
    tool: str,
) -> Callable[[HandsContext, CommentPrAction | CommentIssueAction], dict[str, Any]]:
    """The reviewer's findings go on the pull request, the PM's summary on
    the ticket. GitHub serves both from one endpoint; the gateway polices
    them as two tools, so each role holds only the one its blueprint
    grants. Either way the posted comment's URL is a recorded fact."""

    def comment(
        ctx: HandsContext, a: CommentPrAction | CommentIssueAction
    ) -> dict[str, Any]:
        result = ctx.bridge.call(
            tool,
            {
                "run_id": ctx.run_id,
                "repo": ctx.settings.target_repo,
                "number": int(a.number),
                "body": a.body,
            },
        )
        if result.get("url"):
            ctx.records.comment_urls.append(result["url"])
        return result

    return comment


def _as_json(action: Action, result: dict[str, Any]) -> str:
    return json.dumps(result)


@dataclass(frozen=True)
class ToolSpec:
    """One model-facing tool: what the model is told, the schema it fills,
    the gateway call it resolves to, and how the result is shown back.
    `read_only` is declared once here and drives both the SDK's locking
    (a batch of reads may run concurrently — see `brain.OrderedReadsExecutor`)
    and its schema: read-only tools carry no `security_risk` parameter."""

    description: str
    action: type[Action]
    call: Callable[[HandsContext, Any], dict[str, Any]]
    read_only: bool = False
    render: Callable[[Any, dict[str, Any]], str] = _as_json


_TOOLS: dict[str, ToolSpec] = {
    "list_dir": ToolSpec(
        "List entries of a directory inside the workspace.",
        ListDirAction,
        _list_dir,
        read_only=True,
    ),
    "read_file": ToolSpec(
        "Read a text file from the workspace, as numbered lines.",
        ReadFileAction,
        _read_file,
        read_only=True,
        render=_render_read,
    ),
    "write_file": ToolSpec(
        "Write (create or overwrite) a whole text file in the workspace. For "
        "changes to an existing file prefer edit_file.",
        WriteFileAction,
        _write_file,
    ),
    "edit_file": ToolSpec(
        "Edit part of an existing file: str_replace one exact occurrence of "
        "old_str with new_str, or insert new_str after line insert_line. "
        "Read the file first; the reply shows the edited lines.",
        EditFileAction,
        _edit_file,
        render=_render_edit,
    ),
    "exec": ToolSpec(
        "Run a shell command in the workspace root (e.g. git, ls, python).",
        ExecAction,
        _exec,
    ),
    "run_tests": ToolSpec(
        "Run the repository's test suite in your sandbox.",
        RunTestsAction,
        _run_tests,
    ),
    "open_pr": ToolSpec(
        "Deliver the finished work: bundle the given branch and open a pull "
        "request against the target repository. Commit your changes on the "
        "branch first (via exec).",
        OpenPrAction,
        _open_pr,
    ),
    "create_issue": ToolSpec(
        "File a ticket on the target repository's issue tracker. Use one "
        "ticket per well-scoped piece of work.",
        CreateIssueAction,
        _create_issue,
    ),
    "read_issues": ToolSpec(
        "Read the open ticket backlog (or one ticket by number).",
        ReadIssuesAction,
        _read_issues,
        read_only=True,
    ),
    "read_pr": ToolSpec(
        "Read a pull request: metadata plus the changed files with their patches.",
        ReadPrAction,
        _read_pr,
        read_only=True,
    ),
    "comment_pr": ToolSpec(
        "Post a review comment on the pull request — your findings, in one comment.",
        CommentPrAction,
        _comment_via("git.comment"),
    ),
    "comment_issue": ToolSpec(
        "Post your one-paragraph summary of what you set up as a comment on "
        "a ticket you filed. Every ticket you file gets one.",
        CommentIssueAction,
        _comment_via("issues.comment"),
    ),
    "verdict": ToolSpec(
        "Deliver your verdict on the pull request after posting your "
        "findings: pass or fail, with a one-line summary. A review without "
        "a verdict is a failed dispatch.",
        VerdictAction,
        _verdict,
    ),
}

_READ_ONLY = ToolAnnotations(
    readOnlyHint=True, destructiveHint=False, idempotentHint=True
)


class _GatewayExecutor(ToolExecutor):
    def __init__(self, ctx: HandsContext, spec: ToolSpec) -> None:
        self._ctx = ctx
        self._spec = spec

    def __call__(self, action: Action, conversation: Any = None) -> GatewayObservation:
        try:
            result = self._spec.call(self._ctx, action)
        except ToolCallError as exc:
            # Policy denials and malformed calls feed back as the tool
            # result — the model sees the rule name and recovers.
            return GatewayObservation(output=f"ERROR: {exc}")
        return GatewayObservation(output=self._spec.render(action, result))


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


def _declared_resources(self: ToolDefinition, action: Action) -> DeclaredResources:
    # A read touches no state another call could race on, so it declares
    # none and two of them may run at once. Anything else leaves the
    # declaration to the SDK, whose default is a mutex per tool name.
    read_only = bool(self.annotations and self.annotations.readOnlyHint)
    return DeclaredResources(keys=(), declared=read_only)


_DEFINITION_CLASSES: dict[str, type[ToolDefinition]] = {
    tool_name: type(
        _definition_class_name(tool_name),
        (ToolDefinition,),
        {
            "create": classmethod(_unused_create),
            "declared_resources": _declared_resources,
        },
    )
    for tool_name in _TOOLS
}
for _tool_name, _cls in _DEFINITION_CLASSES.items():
    if _cls.name != _tool_name:
        raise RuntimeError(
            f"tool class {_cls.__name__} resolves to {_cls.name!r}, not {_tool_name!r}"
        )


class PoseidonHands(ToolDefinition):
    """Registered factory: `Tool(name="PoseidonHands", params={"names":
    [...]})` resolves to the named gateway tools, bound to the dispatch's
    `HandsContext`."""

    @classmethod
    def create(cls, conv_state: Any = None, **params: Any) -> list[ToolDefinition]:
        ctx = current_context()
        tools = []
        for name in params["names"]:
            spec = _TOOLS[name]
            tools.append(
                _DEFINITION_CLASSES[name](
                    description=spec.description,
                    action_type=spec.action,
                    observation_type=GatewayObservation,
                    executor=_GatewayExecutor(ctx, spec),
                    annotations=_READ_ONLY if spec.read_only else None,
                )
            )
        return tools


register_tool("PoseidonHands", PoseidonHands)


def hands(names: list[str]) -> list[Tool]:
    """The Agent `tools` entry for one procedure."""
    return [Tool(name="PoseidonHands", params={"names": names})]
