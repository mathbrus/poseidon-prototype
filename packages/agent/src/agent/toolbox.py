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
        """Execute one model tool call; raises ToolCallError on gateway deny/failure."""
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
            case _:
                raise ToolCallError(f"unknown tool: {call.name}")


def tool_result_message(call: ToolCall, result: dict[str, Any] | str) -> dict[str, Any]:
    content = result if isinstance(result, str) else json.dumps(result)
    return {"role": "tool", "tool_call_id": call.id, "content": content}
