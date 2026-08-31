"""OpenAI-compatible HTTP surface over the deterministic brain.

Impersonates the LiteLLM proxy for free e2e runs: same service name, port,
`/chat/completions` route (the agent's AsyncOpenAI client posts without the
`/v1` prefix; the alias keeps standard clients working), the OpenAI
Responses routes (`/responses`, `/v1/responses` — SDKs auto-select that
surface for models that list it, e.g. gpt-5*) and the
`/health/liveliness` probe the llm-proxy Deployment template expects.
Requests are not authenticated — the real proxy ignores the agent's
placeholder bearer token too, and this service never leaves the dev cluster.
Every completion carries the `x-litellm-response-cost` header the real
proxy sets: the gateway meters spend from what the provider reports, so
this provider reports — deterministically, at a fixed rate per token.

The Responses routes are a wire-shape adapter only: the request's
`instructions`/`input` items are translated into the chat `messages` the
brain already understands, and the brain's completion is rendered back as
Responses output items. Scenario logic stays in `brain.py`, untouched.
"""

import json
import uuid
from typing import Any

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict

from mockllm.brain import MockCompletion, next_completion


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None


def _usage(
    messages: list[dict[str, Any]], message: dict[str, Any]
) -> dict[str, Any]:
    """Deterministic token estimate (~4 chars/token): nonzero so the LLM
    gateway's metering and budget hard-stop have real usage
    to account, stable so e2e assertions can rely on it. The details
    blocks are fixed fractions of their totals — valid subsets, nonzero
    for any realistic call, so tests can prove the breakdown flows all
    the way to the ledger."""
    prompt = sum(len(str(m.get("content") or "")) for m in messages) // 4 + 1
    completion = len(str(message.get("content") or "")) // 4 + 1
    for call in message.get("tool_calls") or []:
        completion += len(call["function"]["arguments"]) // 4
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": prompt + completion,
        "prompt_tokens_details": {"cached_tokens": prompt // 2},
        "completion_tokens_details": {"reasoning_tokens": completion // 2},
    }


def _response(
    model: str, messages: list[dict[str, Any]], completion: MockCompletion
) -> dict[str, Any]:
    message: dict[str, Any] = {"role": "assistant", "content": completion.content}
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": f"call-{uuid.uuid4().hex[:12]}",
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in completion.tool_calls
        ]
    return {
        "id": f"mock-{uuid.uuid4().hex[:12]}",
        "object": "chat.completion",
        "created": 0,
        "model": model,
        "choices": [
            {
                "index": 0,
                "message": message,
                "finish_reason": "tool_calls" if completion.tool_calls else "stop",
            }
        ],
        "usage": _usage(messages, message),
    }


class ResponsesRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    input: str | list[dict[str, Any]]
    instructions: str | None = None
    tools: list[dict[str, Any]] | None = None


def _chat_tools(tools: list[dict[str, Any]] | None) -> list[dict[str, Any]] | None:
    """Responses tool schemas arrive flattened
    (`{"type": "function", "name": ..., "parameters": ...}`); normalise to
    the chat shape whose `function.name` the brain's phase keying reads."""
    normalised = []
    for tool in tools or []:
        if "function" in tool:
            normalised.append(tool)
        elif tool.get("type") == "function":
            normalised.append(
                {
                    "type": "function",
                    "function": {
                        "name": tool.get("name", ""),
                        "description": tool.get("description", ""),
                        "parameters": tool.get("parameters") or {},
                    },
                }
            )
    return normalised or None


def _chat_messages(request: ResponsesRequest) -> list[dict[str, Any]]:
    """Responses `instructions` + `input` items -> the chat `messages` the
    brain keys on. Message items keep their content blocks (`input_text` /
    `output_text` both carry `text`, which `_content_text` reads);
    `function_call` items become assistant tool calls so `_made_calls`
    counts them; `function_call_output` items become tool observations so
    `_pr_opened` sees them. Reasoning items carry nothing the brain uses."""
    messages: list[dict[str, Any]] = []
    if request.instructions:
        messages.append({"role": "system", "content": request.instructions})
    if isinstance(request.input, str):
        messages.append({"role": "user", "content": request.input})
        return messages
    for item in request.input:
        kind = item.get("type") or "message"
        if kind == "message":
            messages.append(
                {"role": item.get("role", "user"), "content": item.get("content")}
            )
        elif kind == "function_call":
            messages.append(
                {
                    "role": "assistant",
                    "content": None,
                    "tool_calls": [
                        {
                            "id": item.get("call_id") or item.get("id") or "",
                            "type": "function",
                            "function": {
                                "name": item.get("name", ""),
                                "arguments": item.get("arguments") or "{}",
                            },
                        }
                    ],
                }
            )
        elif kind == "function_call_output":
            output = item.get("output")
            messages.append(
                {
                    "role": "tool",
                    "tool_call_id": item.get("call_id") or "",
                    "content": output if isinstance(output, str) else json.dumps(output),
                }
            )
    return messages


def _responses_body(
    model: str, messages: list[dict[str, Any]], completion: MockCompletion
) -> dict[str, Any]:
    """The brain's completion as an OpenAI Responses body. Built from the
    chat rendering so the tool-call JSON and the deterministic usage
    estimate stay identical across both surfaces."""
    chat = _response(model, messages, completion)
    message = chat["choices"][0]["message"]
    output: list[dict[str, Any]] = []
    if message.get("content"):
        output.append(
            {
                "type": "message",
                "id": f"msg-{uuid.uuid4().hex[:12]}",
                "role": "assistant",
                "status": "completed",
                "content": [
                    {
                        "type": "output_text",
                        "text": message["content"],
                        "annotations": [],
                    }
                ],
            }
        )
    for call in message.get("tool_calls") or []:
        output.append(
            {
                "type": "function_call",
                "id": f"fc-{call['id']}",
                "call_id": call["id"],
                "name": call["function"]["name"],
                "arguments": call["function"]["arguments"],
                "status": "completed",
            }
        )
    usage = chat["usage"]
    return {
        "id": chat["id"],
        "object": "response",
        "created_at": 0,
        "model": model,
        "status": "completed",
        "output": output,
        "parallel_tool_calls": True,
        "tool_choice": "auto",
        "tools": [],
        "usage": {
            "input_tokens": usage["prompt_tokens"],
            "output_tokens": usage["completion_tokens"],
            "total_tokens": usage["total_tokens"],
            "input_tokens_details": usage["prompt_tokens_details"],
            "output_tokens_details": {
                "reasoning_tokens": usage["completion_tokens_details"][
                    "reasoning_tokens"
                ]
            },
        },
    }


# What this fake provider charges per 1K tokens. The gateway meters
# whatever the header reports; the rate only has to be stable and nonzero
# so budget e2e stories (tiny caps, spend ceilings) stay deterministic.
COST_PER_1K_TOKENS = 0.002


def _priced(body: dict[str, Any]) -> JSONResponse:
    """The completion wrapped with the cost header LiteLLM's proxy sets."""
    cost = body["usage"]["total_tokens"] / 1000 * COST_PER_1K_TOKENS
    return JSONResponse(body, headers={"x-litellm-response-cost": f"{cost:.10f}"})


def create_app() -> FastAPI:
    app = FastAPI(title="poseidon-mockllm")

    @app.get("/health/liveliness")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat/completions")
    @app.post("/v1/chat/completions")
    def chat(request: ChatRequest) -> JSONResponse:
        return _priced(
            _response(
                request.model,
                request.messages,
                next_completion(request.messages, request.tools),
            )
        )

    @app.post("/responses")
    @app.post("/v1/responses")
    def responses(request: ResponsesRequest) -> JSONResponse:
        messages = _chat_messages(request)
        return _priced(
            _responses_body(
                request.model,
                messages,
                next_completion(messages, _chat_tools(request.tools)),
            )
        )

    return app


app = create_app()
