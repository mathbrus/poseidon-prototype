"""Activity-side clients: the gateway (MCP + admin endpoint, the worker's
projected identity token) and the agent's C6 HTTP API. Thin by design —
process-level retries, timeouts and cancellation belong to the workflow,
not here."""

import asyncio
import base64
import contextlib
import json
from contextlib import AsyncExitStack
from typing import Any

import httpx
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


class GatewayError(Exception):
    pass


def _is_spurious_cancel(exc: BaseException) -> bool:
    """True for a CancelledError that no one asked for.

    `Task.cancelling()` counts outstanding cancel requests against *this*
    task; zero means the cancellation was manufactured inside the transport
    rather than requested by the workflow, so converting it to an error
    loses nothing and keeps real cancellation working.
    """
    if not isinstance(exc, asyncio.CancelledError):
        return False
    task = asyncio.current_task()
    return task is not None and task.cancelling() == 0


class AgentError(Exception):
    pass


class AgentRunLost(AgentError):
    """The agent no longer knows the run (its in-memory state restarted);
    the dispatch activity retries from the POST."""


class GatewayClient:
    """MCP session for `repo.archive` plus the non-MCP admin registration,
    both authenticated with the token the caller passes (the worker's
    projected service-account token since plan step 11)."""

    def __init__(self, url: str, bearer_token: str) -> None:
        self._base = url.rstrip("/")
        self._token = bearer_token
        self._stack = AsyncExitStack()
        self._http: httpx.AsyncClient | None = None
        self._session: ClientSession | None = None

    async def __aenter__(self) -> "GatewayClient":
        try:
            self._http = await self._stack.enter_async_context(
                httpx.AsyncClient(
                    headers={"Authorization": f"Bearer {self._token}"},
                    timeout=httpx.Timeout(30.0, read=660.0),
                )
            )
            # Locked mcp yields (read, write, get_session_id); the session id
            # is the server's, and nothing here needs it.
            read, write, _get_session_id = await self._stack.enter_async_context(
                streamable_http_client(self._base + "/mcp", http_client=self._http)
            )
            self._session = await self._stack.enter_async_context(
                ClientSession(read, write)
            )
            await self._session.initialize()
        except BaseException as exc:
            # Unwind in the task that opened the stack. Left to the garbage
            # collector instead, the transport's async generator is finalized
            # on some other task and anyio raises "attempted to exit cancel
            # scope in a different task", which buries the real error and
            # leaves the caller hanging until its activity timeout.
            await self._close()
            if _is_spurious_cancel(exc):
                # The transport runs the connection inside an anyio task
                # group, so a plain connection failure can surface as this
                # task group cancelling itself. Callers must be able to
                # handle "the gateway is unreachable" as an ordinary error —
                # otherwise a compensation path that only catches Exception
                # dies instead of finalizing the run.
                raise GatewayError(f"gateway connection failed: {exc!r}") from exc
            if isinstance(exc, Exception):
                raise GatewayError(f"gateway connection failed: {exc}") from exc
            raise  # a genuine cancellation belongs to the caller
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._close()

    async def _close(self) -> None:
        # Teardown errors must never mask the outcome the caller is
        # reporting — a half-open transport is the process's problem, not
        # the run's.
        with contextlib.suppress(Exception):
            await self._stack.aclose()
        self._http = None
        self._session = None

    async def fetch_workspace(self, run_id: str, repo: str, ref: str) -> bytes:
        assert self._session is not None, "use `async with GatewayClient(...)`"
        result = await self._session.call_tool(
            "repo.archive", {"run_id": run_id, "repo": repo, "ref": ref}
        )
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        if result.isError:
            raise GatewayError(text or "repo.archive failed")
        return base64.b64decode(json.loads(text)["archive_b64"])

    async def register_sandbox(
        self,
        run_id: str,
        url: str,
        namespace: str | None = None,
        job_name: str | None = None,
    ) -> None:
        assert self._http is not None, "use `async with GatewayClient(...)`"
        resp = await self._http.post(
            self._base + "/admin/sandboxes",
            json={
                "run_id": run_id,
                "url": url,
                "namespace": namespace,
                "job_name": job_name,
            },
        )
        if resp.status_code != 200:
            raise GatewayError(
                f"sandbox registration failed: {resp.status_code} {resp.text}"
            )

    async def unregister_sandbox(self, run_id: str) -> None:
        """Retire the registration at teardown so a later call for this run
        finds nothing rather than an address someone else now answers."""
        assert self._http is not None, "use `async with GatewayClient(...)`"
        resp = await self._http.request(
            "DELETE", self._base + "/admin/sandboxes", json={"run_id": run_id}
        )
        if resp.status_code != 200:
            raise GatewayError(
                f"sandbox unregistration failed: {resp.status_code} {resp.text}"
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
