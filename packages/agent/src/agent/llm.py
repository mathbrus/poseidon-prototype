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


class BudgetExhausted(Exception):
    """The LLM gateway hard-stopped this run's spend (§4.8). The message
    carries the gateway's `budget_exhausted` marker so the workflow can
    distinguish a budget stop from an ordinary failure (§2b: the workflow
    observes the stop through this surfaced error)."""


class OpenAICompatibleLLM:
    """Talks to the LLM gateway (plan step 14): authenticated with the
    pod's projected workload token, every call attributed to its run. The
    gateway meters usage, routes the model by the caller's tier (the
    `model` field sent here is advisory at best), and holds the only route
    to the provider keys."""

    def __init__(self, base_url: str, model: str, token: str, run_id: str) -> None:
        self._model = model
        self._client = AsyncOpenAI(
            base_url=base_url,
            api_key=token,
            default_headers={"X-Poseidon-Run-Id": run_id},
        )

    async def complete(
        self, messages: list[dict[str, Any]], tools: list[dict[str, Any]] | None = None
    ) -> Completion:
        from openai import APIStatusError

        extra: dict[str, Any] = {"tools": tools} if tools else {}
        try:
            response = await self._client.chat.completions.create(
                model=self._model, messages=messages, **extra
            )
        except APIStatusError as exc:
            if "budget_exhausted" in str(exc.body or exc):
                raise BudgetExhausted(f"budget_exhausted: {exc}") from exc
            raise
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
