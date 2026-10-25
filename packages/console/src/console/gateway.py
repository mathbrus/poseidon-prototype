"""The console's two calls at the MCP gateway: the read of the company's
documents, and the purge of its repository when a person deletes the
company with `--purge-remote`.

The console holds no git credential. It reads a document the way the
platform does — `repo.read_file` at the base ref, through the gateway —
and purges with `repo.purge`, but under its own identity (the `console`
lifecycle, from its projected token for the gateway's audience), which
the company's bundle allows those two calls, on that repository, at that
ref, and nothing else. The gateway decides and audits each call under
`console`; the person's name is not forwarded, because the gateway trusts
no forwarded identity.
"""

import asyncio
import json
from contextlib import AsyncExitStack
from pathlib import Path
from typing import Any, Protocol

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from blueprint.tools import REPO_PURGE
from common.topology import TOKEN_FILE, mcp_gateway_url

READ_FILE = "repo.read_file"
# How long a call may take: a read is one request to the git host; a
# purge is one per open pull request, issue and branch.
READ_TIMEOUT_S = 30.0
PURGE_TIMEOUT_S = 300.0


class GatewayUnavailable(Exception):
    """The gateway could not be reached, or refused the call."""


class Gateway(Protocol):
    """What the service needs from the gateway — a seam the tests fake."""

    async def read(self, repo: str, ref: str, path: str) -> dict[str, Any]:
        """`{present, text}` for one file at `ref`."""
        ...

    async def purge(self, repo: str, base_ref: str) -> dict[str, Any]:
        """Reset the repository in place; what was closed and deleted."""
        ...


class McpGateway:
    def __init__(self, platform_namespace: str, token_file: str = TOKEN_FILE) -> None:
        self._url = mcp_gateway_url(platform_namespace) + "/mcp"
        self._token_file = Path(token_file)

    async def read(self, repo: str, ref: str, path: str) -> dict[str, Any]:
        args = {"run_id": "", "repo": repo, "ref": ref, "path": path}
        return await self._call(READ_FILE, args, READ_TIMEOUT_S)

    async def purge(self, repo: str, base_ref: str) -> dict[str, Any]:
        args = {"run_id": "", "repo": repo, "base_ref": base_ref}
        return await self._call(REPO_PURGE, args, PURGE_TIMEOUT_S)

    async def _call(
        self, tool: str, args: dict[str, Any], timeout_s: float
    ) -> dict[str, Any]:
        # The kubelet rotates the projected token; read it per call.
        token = self._token_file.read_text().strip()
        try:
            async with AsyncExitStack() as stack:
                http = await stack.enter_async_context(
                    httpx.AsyncClient(
                        headers={"Authorization": f"Bearer {token}"},
                        timeout=httpx.Timeout(timeout_s),
                    )
                )
                read, write, _ = await stack.enter_async_context(
                    streamable_http_client(self._url, http_client=http)
                )
                session = await stack.enter_async_context(ClientSession(read, write))
                await session.initialize()
                result = await session.call_tool(tool, args)
        except Exception as exc:
            raise GatewayUnavailable(f"mcp gateway unreachable: {exc}") from exc
        except asyncio.CancelledError as exc:
            # The transport runs its connection in a task group that can
            # cancel itself when the gateway is unreachable; a cancel this
            # task was never asked for is that failure, not the caller's.
            task = asyncio.current_task()
            if task is None or task.cancelling():
                raise
            raise GatewayUnavailable(f"mcp gateway unreachable: {exc!r}") from exc
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        if result.isError:
            raise GatewayUnavailable(f"mcp gateway refused {tool}: {text}")
        return json.loads(text)
