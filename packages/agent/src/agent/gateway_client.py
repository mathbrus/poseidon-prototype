"""MCP client for the gateway.

The agent host depends only on `call(tool, args) -> dict`; a gateway tool
error (policy denial, backend failure) raises `ToolCallError` carrying the
error text — which includes the OPA rule name on a deny, so the agent host
can surface it to the model.
"""

import json
from contextlib import AsyncExitStack
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class ToolCallError(Exception):
    pass


class Gateway(Protocol):
    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]: ...


class McpGateway:
    """Async-context-managed MCP session against the gateway, bearer-authed."""

    def __init__(self, url: str, bearer_token: str) -> None:
        self._url = url.rstrip("/") + "/mcp"
        self._token = bearer_token
        self._stack = AsyncExitStack()
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "McpGateway":
        client = await self._stack.enter_async_context(
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(30.0, read=660.0),
            )
        )
        read, write, _ = await self._stack.enter_async_context(
            streamable_http_client(self._url, http_client=client)
        )
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.aclose()
        self._session = None

    async def call(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self._session is not None, "use `async with McpGateway(...)`"
        result = await self._session.call_tool(tool, args)
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        if result.isError:
            raise ToolCallError(text or f"tool {tool} failed")
        return json.loads(text)
