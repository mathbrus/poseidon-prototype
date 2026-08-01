"""Activity-side clients: the gateway (MCP + admin endpoint, orchestrator
token) and the agent's C6 HTTP API. Thin by design — process-level retries,
timeouts and cancellation belong to the workflow, not here."""

import base64
import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class GatewayError(Exception):
    pass


class AgentError(Exception):
    pass


class AgentRunLost(AgentError):
    """The agent no longer knows the run (its in-memory state restarted);
    the dispatch activity retries from the POST."""


class GatewayClient:
    """MCP session for `repo.archive` plus the non-MCP admin registration,
    both authenticated with the orchestrator bearer token."""

    def __init__(self, url: str, bearer_token: str) -> None:
        self._base = url.rstrip("/")
        self._token = bearer_token
        self._stack = AsyncExitStack()
        self._http: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "GatewayClient":
        self._http = await self._stack.enter_async_context(
            httpx.AsyncClient(
                headers={"Authorization": f"Bearer {self._token}"},
                timeout=httpx.Timeout(30.0, read=660.0),
            )
        )
        # 2-tuple (mcp >= 1.16) or 3-tuple (older) — take the two streams.
        transport = await self._stack.enter_async_context(
            streamable_http_client(self._base + "/mcp", http_client=self._http)
        )
        read, write = transport[0], transport[1]
        self._session = await self._stack.enter_async_context(ClientSession(read, write))
        await self._session.initialize()
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._stack.aclose()
        self._http = None
        self._session = None

    async def fetch_workspace(self, repo: str, ref: str) -> bytes:
        assert self._session is not None, "use `async with GatewayClient(...)`"
        result = await self._session.call_tool("repo.archive", {"repo": repo, "ref": ref})
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        # isError (mcp < 1.16) / is_error (newer).
        if getattr(result, "is_error", None) or getattr(result, "isError", None):
            raise GatewayError(text or "repo.archive failed")
        return base64.b64decode(json.loads(text)["archive_b64"])

    async def register_sandbox(self, run_id: str, url: str) -> None:
        assert self._http is not None, "use `async with GatewayClient(...)`"
        resp = await self._http.post(
            self._base + "/admin/sandboxes", json={"run_id": run_id, "url": url}
        )
        if resp.status_code != 200:
            raise GatewayError(
                f"sandbox registration failed: {resp.status_code} {resp.text}"
            )


class AgentClient:
    """The agent's C6 endpoints: dispatch a task, poll its status."""

    def __init__(self, url: str) -> None:
        self._http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30.0)

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._http.aclose()

    async def dispatch(self, run_id: str, task_text: str) -> None:
        """Idempotent under retry: 409 means the agent already has the run
        (a previous attempt dispatched it before the worker died) — the
        caller proceeds to polling either way."""
        resp = await self._http.post(
            "/task", json={"run_id": run_id, "task_text": task_text}
        )
        if resp.status_code not in (202, 409):
            raise AgentError(f"task dispatch failed: {resp.status_code} {resp.text}")

    async def status(self, run_id: str) -> dict[str, Any]:
        resp = await self._http.get(f"/task/{run_id}")
        if resp.status_code == 404:
            raise AgentRunLost(f"agent lost run {run_id} (restarted?)")
        if resp.status_code != 200:
            raise AgentError(f"status poll failed: {resp.status_code} {resp.text}")
        return resp.json()
