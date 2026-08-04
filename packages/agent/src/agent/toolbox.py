"""Model-facing tools and their mapping onto gateway MCP calls.

The model sees a small tool surface without `run_id` (the toolbox injects it
on every call, per §4.2) and without bundle plumbing: `open_pr` is a composite
that runs `sandbox.git_bundle` then `git.open_pr` so base64 bundles never
enter the model context.

`ToolCallError` from the gateway is caught by the caller (graph), not here.
"""

import json
from typing import Any

from agent.config import AgentSettings
from agent.gateway_client import Gateway, ToolCallError
from agent.llm import ToolCall

_SCHEMAS: dict[str, dict[str, Any]] = {
    "list_dir": {
        "description": "List entries of a directory inside the workspace.",
        "parameters": {"path": {"type": "string", "description": "Directory path, relative to the workspace root."}},
        "required": ["path"],
    },
    "read_file": {
        "description": "Read a text file from the workspace.",
        "parameters": {"path": {"type": "string", "description": "File path, relative to the workspace root."}},
        "required": ["path"],
    },
    "write_file": {
        "description": "Write (create or overwrite) a text file in the workspace.",
        "parameters": {
            "path": {"type": "string", "description": "File path, relative to the workspace root."},
            "content": {"type": "string", "description": "Full new file content."},
        },
        "required": ["path", "content"],
    },
    "exec": {
        "description": "Run a shell command in the workspace root (e.g. git, ls, python).",
        "parameters": {
            "cmd": {"type": "string", "description": "Shell command line."},
            "timeout_s": {"type": "number", "description": "Timeout in seconds (default 60)."},
        },
        "required": ["cmd"],
    },
    # -- PM tools (plan step 17): tickets and crew intent, nothing else --
    "create_issue": {
        "description": (
            "File a ticket on the target repository's issue tracker. Use one "
            "ticket per well-scoped piece of work."
        ),
        "parameters": {
            "title": {"type": "string", "description": "Short, specific ticket title."},
            "body": {"type": "string", "description": "The scoped work: context, acceptance criteria."},
        },
        "required": ["title", "body"],
    },
    "read_issues": {
        "description": "Read the open ticket backlog (or one ticket by number).",
        "parameters": {
            "number": {"type": "number", "description": "Optional issue number; omit for the open backlog."},
        },
        "required": [],
    },
    "request_crew": {
        "description": (
            "Request an ephemeral crew to carry out scoped work. This records "
            "intent for the platform to fulfill — it does not start anything "
            "by itself."
        ),
        "parameters": {
            "crew_template": {"type": "string", "description": "Crew template name, e.g. feature-delivery."},
            "reason": {"type": "string", "description": "Why, referencing the ticket(s)."},
        },
        "required": ["crew_template", "reason"],
    },
    # -- reviewer tools (plan step 20): read the PR, judge, comment --
    "read_pr": {
        "description": (
            "Read a pull request: metadata plus the changed files with "
            "their patches."
        ),
        "parameters": {
            "number": {"type": "number", "description": "Pull request number."},
        },
        "required": ["number"],
    },
    "comment_pr": {
        "description": (
            "Post a review comment on the pull request — your findings, "
            "in one comment."
        ),
        "parameters": {
            "number": {"type": "number", "description": "Pull request number."},
            "body": {"type": "string", "description": "The findings: what you checked, what you found."},
        },
        "required": ["number", "body"],
    },
    "run_tests": {
        "description": "Run the repository's test suite in your sandbox.",
        "parameters": {},
        "required": [],
    },
    "open_pr": {
        "description": (
            "Deliver the finished work: bundle the given branch and open a pull "
            "request against the target repository. Commit your changes on the "
            "branch first (via exec)."
        ),
        "parameters": {
            "branch": {"type": "string", "description": "Branch to deliver, e.g. feature/add-parser."},
            "title": {"type": "string", "description": "Pull request title."},
            "body": {"type": "string", "description": "Pull request description."},
        },
        "required": ["branch", "title", "body"],
    },
}

ACT_TOOLS = ["list_dir", "read_file", "write_file", "exec"]
DELIVER_TOOLS = ACT_TOOLS + ["open_pr"]
PM_TOOLS = ["create_issue", "read_issues", "request_crew"]
# The reviewer (plan step 20): reads and executes, writes nothing but the
# findings comment — mirroring the sdet crew member's compiled scope.
REVIEWER_TOOLS = ["list_dir", "read_file", "exec", "run_tests", "read_pr", "comment_pr"]


def tool_specs(names: list[str]) -> list[dict[str, Any]]:
    """OpenAI function-calling specs for the named tools."""
    specs = []
    for name in names:
        schema = _SCHEMAS[name]
        specs.append(
            {
                "type": "function",
                "function": {
                    "name": name,
                    "description": schema["description"],
                    "parameters": {
                        "type": "object",
                        "properties": schema["parameters"],
                        "required": schema["required"],
                    },
                },
            }
        )
    return specs


class Toolbox:
    def __init__(self, gateway: Gateway, settings: AgentSettings, run_id: str) -> None:
        self._gateway = gateway
        self._settings = settings
        self._run_id = run_id

    async def run_tests(self) -> dict[str, Any]:
        return await self._gateway.call("sandbox.run_tests", {"run_id": self._run_id})

    async def execute(self, call: ToolCall) -> dict[str, Any]:
        """Execute one model tool call; raises ToolCallError on gateway
        deny/failure and on malformed calls (the model does not always honor
        the schema), so the graph can feed the error back to it."""
        try:
            return await self._execute(call)
        except KeyError as exc:
            raise ToolCallError(
                f"tool {call.name} is missing required argument {exc}"
            ) from exc

    async def _execute(self, call: ToolCall) -> dict[str, Any]:
        args = call.arguments
        run_id = self._run_id
        match call.name:
            case "list_dir":
                return await self._gateway.call(
                    "sandbox.list_dir", {"run_id": run_id, "path": args["path"]}
                )
            case "read_file":
                return await self._gateway.call(
                    "sandbox.read_file", {"run_id": run_id, "path": args["path"]}
                )
            case "write_file":
                return await self._gateway.call(
                    "sandbox.write_file",
                    {"run_id": run_id, "path": args["path"], "content": args["content"]},
                )
            case "exec":
                return await self._gateway.call(
                    "sandbox.exec",
                    {
                        "run_id": run_id,
                        "cmd": args["cmd"],
                        "timeout_s": args.get("timeout_s", 60),
                    },
                )
            case "open_pr":
                bundle = await self._gateway.call(
                    "sandbox.git_bundle", {"run_id": run_id, "branch": args["branch"]}
                )
                return await self._gateway.call(
                    "git.open_pr",
                    {
                        "run_id": run_id,
                        "repo": self._settings.target_repo,
                        "branch": args["branch"],
                        "title": args["title"],
                        "body": args["body"],
                        "bundle_b64": bundle["bundle_b64"],
                    },
                )
            case "create_issue":
                return await self._gateway.call(
                    "issues.create",
                    {"run_id": run_id, "repo": self._settings.target_repo,
                     "title": args["title"], "body": args["body"]},
                )
            case "read_issues":
                return await self._gateway.call(
                    "issues.read",
                    {"run_id": run_id, "repo": self._settings.target_repo,
                     "number": args.get("number")},
                )
            case "read_pr":
                return await self._gateway.call(
                    "git.read",
                    {"run_id": run_id, "repo": self._settings.target_repo,
                     "number": int(args["number"])},
                )
            case "comment_pr":
                return await self._gateway.call(
                    "git.comment",
                    {"run_id": run_id, "repo": self._settings.target_repo,
                     "number": int(args["number"]), "body": args["body"]},
                )
            case "run_tests":
                return await self.run_tests()
            case "request_crew":
                return await self._gateway.call(
                    "crew.request",
                    {"run_id": run_id, "crew_template": args["crew_template"],
                     "reason": args["reason"]},
                )
            case _:
                raise ToolCallError(f"unknown tool: {call.name}")


def tool_result_message(call: ToolCall, result: dict[str, Any] | str) -> dict[str, Any]:
    content = result if isinstance(result, str) else json.dumps(result)
    return {"role": "tool", "tool_call_id": call.id, "content": content}
