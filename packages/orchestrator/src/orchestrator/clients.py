"""Activity-side clients: the gateway (MCP + admin endpoint, the worker's
projected identity token) and the agent's dispatch HTTP API. Thin by design —
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
from temporalio import activity

# How often a dispatch polls the agent for its terminal status.
POLL_INTERVAL_S = 2.0


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
    projected service-account token)."""

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

    async def fetch_workspace(
        self,
        run_id: str,
        repo: str,
        ref: str,
        overlay_ref: str | None = None,
        overlay_paths: list[str] | None = None,
    ) -> bytes:
        """Archive `ref` — optionally with `overlay_paths` mirrored from
        `overlay_ref`, the composed tree the contract verify runs on."""
        assert self._session is not None, "use `async with GatewayClient(...)`"
        args: dict[str, Any] = {"run_id": run_id, "repo": repo, "ref": ref}
        if overlay_ref:
            args["overlay_ref"] = overlay_ref
            args["overlay_paths"] = overlay_paths or []
        result = await self._tool("repo.archive", args)
        return base64.b64decode(result["archive_b64"])

    async def _tool(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        assert self._session is not None, "use `async with GatewayClient(...)`"
        result = await self._session.call_tool(name, args)
        text = "".join(
            block.text for block in result.content if getattr(block, "text", None)
        )
        if result.isError:
            raise GatewayError(text or f"{name} failed")
        return json.loads(text)

    async def run_tests(
        self, run_id: str, paths: list[str] | None = None
    ) -> dict[str, Any]:
        """The platform verify gate:
        `sandbox.run_tests` under the orchestrator's own identity — the
        gate of record after the brain's completion claim. `paths` scopes
        the run to named test paths (the contract-verify call)."""
        args: dict[str, Any] = {"run_id": run_id}
        if paths:
            args["paths"] = paths
        return await self._tool("sandbox.run_tests", args)

    async def read_pr(self, run_id: str, repo: str, number: int) -> dict[str, Any]:
        """PR metadata (state and head ref included) under the platform
        identity — how the review workflow resolves the ref the review
        sandbox is provisioned from, and how the heartbeat checks that a
        delivered PR is still the world's to review."""
        return await self._tool(
            "git.read", {"run_id": run_id, "repo": repo, "number": number}
        )

    async def read_document(
        self, run_id: str, repo: str, ref: str, path: str
    ) -> dict[str, Any]:
        """One file of the repository at `ref` under the platform identity
        — `{present, text}` — how the company's documents are read at
        `base_ref` before a tick or a run, never from a sandbox."""
        return await self._tool(
            "repo.read_file", {"run_id": run_id, "repo": repo, "ref": ref, "path": path}
        )

    async def present_paths(
        self, run_id: str, repo: str, ref: str, paths: list[str]
    ) -> list[str]:
        """The subset of `paths` the repository holds at `ref`, under the
        platform identity — how the review learns whether the declared
        contract tree exists on `base_ref` before composing a run on it."""
        result = await self._tool(
            "repo.present",
            {"run_id": run_id, "repo": repo, "ref": ref, "paths": paths},
        )
        return list(result.get("present") or [])

    async def read_issue(self, run_id: str, repo: str, number: int) -> dict[str, Any]:
        """One issue by number for the implement workflow — how the
        platform resolves the ticket an issue-driven run implements."""
        result = await self._tool(
            "issues.read", {"run_id": run_id, "repo": repo, "number": number}
        )
        return result["issues"][0]

    async def create_issue(
        self, run_id: str, repo: str, title: str, body: str, rationale: str
    ) -> dict[str, Any]:
        """File a ticket under the platform identity — the rework loop's
        write (a failed policy-gated review puts its findings back into the
        same backlog the conductor already consumes) and the bootstrap's.
        The rationale is the platform's reason, recorded with the call."""
        return await self._tool(
            "issues.create",
            {
                "run_id": run_id,
                "repo": repo,
                "title": title,
                "body": body,
                "rationale": rationale,
            },
        )

    async def close_issue(
        self, run_id: str, repo: str, number: int, rationale: str
    ) -> dict[str, Any]:
        """Close a ticket under the platform identity — the consequence of
        a merge, never a role's act. The rationale names the merge."""
        return await self._tool(
            "issues.close",
            {"run_id": run_id, "repo": repo, "number": number, "rationale": rationale},
        )

    async def read_open_issues(self, run_id: str, repo: str) -> list[dict[str, Any]]:
        """The open backlog for the heartbeat's tick. Only numbers,
        titles and URLs feed the decision — issue bodies are untrusted
        input the implement run resolves itself."""
        result = await self._tool("issues.read", {"run_id": run_id, "repo": repo})
        return list(result["issues"])

    async def comment_pr(
        self, run_id: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """A platform comment on a PR — the oracle-edit warning the human
        reads before resolving the merge signal."""
        return await self._tool(
            "git.comment",
            {"run_id": run_id, "repo": repo, "number": number, "body": body},
        )

    async def comment_issue(
        self, run_id: str, repo: str, number: int, body: str
    ) -> dict[str, Any]:
        """A platform comment on an issue — how the world learns that the
        heartbeat spent its last attempt on a ticket."""
        return await self._tool(
            "issues.comment",
            {"run_id": run_id, "repo": repo, "number": number, "body": body},
        )

    async def merge_pr(
        self, run_id: str, repo: str, number: int, sha: str, method: str = "merge"
    ) -> dict[str, Any]:
        """The post-gate merge of head `sha`: only ever called by the review
        workflow after the merge gate resolved to merge, under the
        orchestrator's platform identity — policy restricts `git.merge_pr`
        to exactly that identity."""
        return await self._tool(
            "git.merge_pr",
            {
                "run_id": run_id,
                "repo": repo,
                "number": number,
                "sha": sha,
                "method": method,
            },
        )

    async def close_pr(
        self, run_id: str, repo: str, number: int, rationale: str
    ) -> dict[str, Any]:
        """Close a pull request unmerged under the platform identity — the
        rework loop's act once a failed review's findings are a ticket.
        The rationale names the ticket that supersedes it."""
        return await self._tool(
            "git.close_pr",
            {"run_id": run_id, "repo": repo, "number": number, "rationale": rationale},
        )

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


def pr_settled(pr: dict[str, Any]) -> str | None:
    """Why a pull request is no longer reviewable, from the metadata
    `read_pr` returns: `"merged"`, `"closed"` (closed without merging), or
    None while it is open. The git host is the authority on this — the
    registry only knows which PRs the company delivered and which reviews
    it ran, never what a human or another harness did to them since."""
    if pr.get("merged"):
        return "merged"
    if str(pr.get("state") or "") != "open":
        return "closed"
    return None


class AgentClient:
    """The agent's dispatch endpoints: dispatch a task, poll its status."""

    def __init__(self, url: str) -> None:
        self._http = httpx.AsyncClient(base_url=url.rstrip("/"), timeout=30.0)

    async def __aenter__(self) -> "AgentClient":
        return self

    async def __aexit__(self, *exc_info: Any) -> None:
        await self._http.aclose()

    async def dispatch(
        self,
        run_id: str,
        procedure: str,
        task_text: str,
        bounds: str,
        documents: dict[str, str] | None = None,
    ) -> None:
        """Ask the agent to run `procedure` on `task_text`, with the bounds
        the platform rendered for the company and its documents as
        untrusted context. Idempotent under retry: 409 means the agent
        already has the run (a previous attempt dispatched it before the
        worker died) — the caller proceeds to polling either way. No
        credential rides the dispatch — every pod holds its own projected
        token."""
        payload = {
            "run_id": run_id,
            "procedure": procedure,
            "task_text": task_text,
            "bounds": bounds,
            "documents": documents or {},
        }
        resp = await self._http.post("/task", json=payload)
        if resp.status_code not in (202, 409):
            raise AgentError(f"task dispatch failed: {resp.status_code} {resp.text}")

    async def status(self, run_id: str) -> dict[str, Any]:
        resp = await self._http.get(f"/task/{run_id}")
        if resp.status_code == 404:
            raise AgentRunLost(f"agent lost run {run_id} (restarted?)")
        if resp.status_code != 200:
            raise AgentError(f"status poll failed: {resp.status_code} {resp.text}")
        return resp.json()


async def dispatch_and_poll(
    url: str,
    run_id: str,
    procedure: str,
    task_text: str,
    bounds: str,
    documents: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Dispatch a procedure to the agent at `url` and poll it to a terminal
    status, heartbeating every poll so a dead worker is noticed fast. The
    pod's own projected token is its credential — nothing rides this call
    or the workflow history. Callable outside an activity (unit tests): the
    heartbeat is skipped when there is no activity context."""
    async with AgentClient(url) as agent:
        await agent.dispatch(run_id, procedure, task_text, bounds, documents)
        while True:
            if activity.in_activity():
                activity.heartbeat()
            status = await agent.status(run_id)
            if status["status"] != "running":
                return status
            await asyncio.sleep(POLL_INTERVAL_S)
