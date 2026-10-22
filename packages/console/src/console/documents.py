"""The console's one read at the MCP gateway: the company's documents.

The console holds no git credential. It reads a document the way the
platform does — `repo.read_file` at the base ref, through the gateway —
but under its own identity (the `console` lifecycle, from its projected
token for the gateway's audience), which the company's bundle allows that
one tool, on that repository, at that ref, of the two document paths, and
nothing else. The gateway decides and audits the call under `console`;
the person's name is not forwarded, because the gateway trusts no
forwarded identity.
"""

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from common.topology import TOKEN_FILE, mcp_gateway_url

READ_FILE = "repo.read_file"


class DocumentsUnavailable(Exception):
    """The gateway could not be reached, or refused the read."""


class Documents(Protocol):
    """What the service needs to read a document — a seam the tests fake."""

    async def read(self, repo: str, ref: str, path: str) -> dict[str, Any]:
        """`{present, text}` for one file at `ref`."""
        ...


class GatewayDocuments:
    def __init__(self, platform_namespace: str, token_file: str = TOKEN_FILE) -> None:
        self._url = mcp_gateway_url(platform_namespace) + "/mcp"
        self._token_file = Path(token_file)

    async def read(self, repo: str, ref: str, path: str) -> dict[str, Any]:
        # The kubelet rotates the projected token; read it per call.
        token = self._token_file.read_text().strip()
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=httpx.Timeout(30.0),
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.call_tool(
                    READ_FILE, {"run_id": "", "repo": repo, "ref": ref, "path": path}
                )
        except Exception as exc:
            raise DocumentsUnavailable(f"mcp gateway unreachable: {exc}") from exc
        except asyncio.CancelledError as exc:
            # The transport runs its connection in a task group that can
            # cancel itself when the gateway is unreachable; a cancel this
            # task was never asked for is that failure, not the caller's.
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            raise DocumentsUnavailable(f"mcp gateway unreachable: {exc!r}") from exc
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        if result.isError:
            raise DocumentsUnavailable(f"mcp gateway refused the read: {text}")
        return json.loads(text)
