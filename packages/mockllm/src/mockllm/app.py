"""OpenAI-compatible HTTP surface over the deterministic brain.

Impersonates the LiteLLM proxy for free e2e runs: same service name, port,
`/chat/completions` route (the agent's AsyncOpenAI client posts without the
`/v1` prefix; the alias keeps standard clients working) and the
`/health/liveliness` probe the llm-proxy Deployment template expects.
Requests are not authenticated — the real proxy ignores the agent's
placeholder bearer token too, and this service never leaves the dev cluster.
"""

import json
import uuid
from typing import Any

from fastapi import FastAPI
from pydantic import BaseModel, ConfigDict

from mockllm.brain import MockCompletion, next_completion


class ChatRequest(BaseModel):
    model_config = ConfigDict(extra="allow")

    model: str
    messages: list[dict[str, Any]]
    tools: list[dict[str, Any]] | None = None


def _response(model: str, completion: MockCompletion) -> dict[str, Any]:
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
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def create_app() -> FastAPI:
    app = FastAPI(title="poseidon-mockllm")

    @app.get("/health/liveliness")
    def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/chat/completions")
    @app.post("/v1/chat/completions")
    def chat(request: ChatRequest) -> dict[str, Any]:
        return _response(request.model, next_completion(request.messages, request.tools))

    return app


app = create_app()
