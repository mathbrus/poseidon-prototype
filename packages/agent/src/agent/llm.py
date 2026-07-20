"""LLM access: OpenAI-compatible chat completions against the LLM proxy.

The graph only depends on `complete(messages, tools) -> Completion`, so tests
substitute a scripted implementation. Messages travel in plain OpenAI chat
format (dicts); `Completion` is the parsed assistant turn.
"""

import json
from typing import Any, Protocol

from openai import AsyncOpenAI
from pydantic import BaseModel, Field


class ToolCall(BaseModel):
    id: str
    name: str
    arguments: dict[str, Any]


class Completion(BaseModel):
    content: str | None = None
    tool_calls: list[ToolCall] = Field(default_factory=list)


class LLM(Protocol):
    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Completion: ...


def assistant_message(completion: Completion) -> dict[str, Any]:
    """Render a Completion back into OpenAI chat-history form."""
    message: dict[str, Any] = {"role": "assistant", "content": completion.content}
    if completion.tool_calls:
        message["tool_calls"] = [
            {
                "id": call.id,
                "type": "function",
                "function": {"name": call.name, "arguments": json.dumps(call.arguments)},
            }
            for call in completion.tool_calls
        ]
    return message


class OpenAICompatibleLLM:
    """Talks to the LLM proxy (LiteLLM); the proxy holds the provider key,
    so the client-side API key is a placeholder."""

    def __init__(self, base_url: str, model: str) -> None:
        self._model = model
        self._client = AsyncOpenAI(base_url=base_url, api_key="poseidon-agent")

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Completion:
        extra: dict[str, Any] = {"tools": tools} if tools else {}
        response = await self._client.chat.completions.create(
            model=self._model, messages=messages, **extra
        )
        message = response.choices[0].message
        tool_calls = [
            ToolCall(
                id=call.id,
                name=call.function.name,
                arguments=json.loads(call.function.arguments or "{}"),
            )
            for call in (message.tool_calls or [])
        ]
        return Completion(content=message.content, tool_calls=tool_calls)
