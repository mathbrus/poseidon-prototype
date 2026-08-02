"""Gateway enforcement pipeline: policy input -> OPA -> audit -> backend.

Every call (MCP tool or admin endpoint) goes through `GatewayService.call`,
which builds the C2 policy input, queries OPA, emits a C1 audit event, and
only on allow executes the backend action.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import datetime, timezone
from typing import Any

import httpx

from common.audit import AuditEvent
from common.telemetry import get_logger
from gateway.config import GatewaySettings
from gateway.git_backend import GitBackend
from gateway.identity import AGENT_ID, Identity
from gateway.opa import OpaClient
from registry import AuditStore, SandboxRegistrations

# tool name -> sandbox tool-server route (contract C4)
SANDBOX_ROUTES = {
    "sandbox.read_file": "/files/read",
    "sandbox.write_file": "/files/write",
    "sandbox.list_dir": "/files/list",
    "sandbox.exec": "/exec",
    "sandbox.run_tests": "/tests",
    "sandbox.git_bundle": "/bundle",
}


class PolicyDenied(Exception):
    def __init__(self, rule: str | None) -> None:
        self.rule = rule or "unknown"
        super().__init__(f"denied by policy rule: {self.rule}")


class BackendError(Exception):
    pass


def args_digest(args: dict[str, Any]) -> str:
    canonical = json.dumps(args, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(canonical.encode()).hexdigest()


class GatewayService:
    def __init__(
        self,
        settings: GatewaySettings,
        opa: OpaClient,
        http_client: httpx.AsyncClient,
        sandboxes: SandboxRegistrations,
        audit: AuditStore,
        git_backend: GitBackend | None = None,
    ) -> None:
        self._settings = settings
        self._opa = opa
        self._http = http_client
        self._git = git_backend or GitBackend(settings, http_client)
        self._log = get_logger("gateway")
        # Registry-backed since plan step 8: an in-memory dict silently
        # orphaned every sandbox call of every in-flight run whenever this
        # process restarted (resolution row 16).
        self._sandboxes = sandboxes
        self._audit_store = audit

    # -- sandbox registry -------------------------------------------------
    # Registry accessors are synchronous psycopg; every use goes through
    # to_thread so a slow or absent Postgres cannot stall the event loop
    # (the pool blocks up to CONNECT_TIMEOUT_S — long enough to fail the
    # liveness probe if it ran on the loop).

    async def register_sandbox(
        self,
        run_id: str,
        url: str,
        namespace: str | None = None,
        job_name: str | None = None,
    ) -> None:
        await asyncio.to_thread(
            self._sandboxes.register, run_id, url.rstrip("/"), namespace, job_name
        )

    async def sandbox_url(self, run_id: str) -> str | None:
        return await asyncio.to_thread(self._sandboxes.url_for, run_id)

    # -- enforcement pipeline ---------------------------------------------

    async def call(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        backend: Callable[[], Awaitable[Any]],
    ) -> Any:
        """Authorize `tool(args)` for `identity`, audit, then run `backend`."""
        run_id = str(args.get("run_id", ""))
        started = time.monotonic()

        policy_input = {
            "agent_id": identity.agent_id,
            "role": identity.role,
            "run_id": run_id,
            "tool": tool,
            "args": args,
        }
        decision = await self._opa.decide(policy_input)

        if not decision.allow:
            await self._audit(identity, tool, args, run_id, "deny", decision.rule, started, None)
            raise PolicyDenied(decision.rule)

        try:
            result = await backend()
        except Exception:
            await self._audit(identity, tool, args, run_id, "allow", decision.rule, started, "error")
            raise
        await self._audit(identity, tool, args, run_id, "allow", decision.rule, started, "ok")
        return result

    async def proxy_sandbox(self, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        """Forward an allowed sandbox.* call to the run's registered tool server."""
        run_id = str(args["run_id"])
        base_url = await self.sandbox_url(run_id)
        if base_url is None:
            raise BackendError(f"no sandbox registered for run_id {run_id!r}")
        payload = {k: v for k, v in args.items() if k != "run_id"}
        resp = await self._http.post(base_url + SANDBOX_ROUTES[tool], json=payload)
        if resp.status_code != 200:
            raise BackendError(f"sandbox returned {resp.status_code}: {resp.text}")
        return resp.json()

    async def register_run(
        self,
        identity: Identity,
        run_id: str,
        url: str,
        namespace: str | None = None,
        job_name: str | None = None,
    ) -> None:
        """Admin action: register a sandbox and push the run assignment to OPA."""

        async def backend() -> None:
            await self.register_sandbox(run_id, url, namespace, job_name)
            await self._opa.assign_run(AGENT_ID, run_id)

        await self.call(
            identity, "/admin/sandboxes", {"run_id": run_id, "url": url}, backend
        )

    async def unregister_run(self, identity: Identity, run_id: str) -> None:
        """Admin action: retire a registration at teardown. The OPA run
        assignment stays until the compiled-policy cutover (plan step 12)
        replaces `data.runs.assignments` with registry-enriched input."""

        async def backend() -> None:
            await asyncio.to_thread(self._sandboxes.terminate, run_id)

        await self.call(
            identity, "/admin/sandboxes.delete", {"run_id": run_id}, backend
        )

    # -- git tools ----------------------------------------------------------

    async def archive_repo(
        self, identity: Identity, run_id: str, repo: str, ref: str
    ) -> Any:
        # run_id rides the args so the workspace fetch is attributable like
        # every other step: without it this event is the one hole in "the
        # whole story for run X" (E4).
        args = {"run_id": run_id, "repo": repo, "ref": ref}
        return await self.call(
            identity, "repo.archive", args, lambda: self._git.archive(repo, ref)
        )

    async def open_pr(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        branch: str,
        title: str,
        body: str,
        bundle_b64: str,
    ) -> Any:
        """Verify + apply the bundle, then authorize with the changed paths.

        The bundle is applied to a platform-side clone *before* the policy
        query so the changed paths can be part of the C2 input (`args.paths`).
        Nothing leaves the gateway until `call` allows the push. A bundle that
        fails verification still produces an audited call (backend error).
        """
        args = {
            "run_id": run_id,
            "repo": repo,
            "branch": branch,
            "title": title,
            "body": body,
            "bundle_b64": bundle_b64,
        }
        try:
            prepared = await self._git.prepare_pr(repo, branch, bundle_b64)
        except Exception as exc:

            async def failing_backend() -> Any:
                raise BackendError(str(exc)) from exc

            return await self.call(identity, "git.open_pr", args, failing_backend)

        try:
            return await self.call(
                identity,
                "git.open_pr",
                {**args, "paths": prepared.paths},
                lambda: self._git.open_pr(prepared, repo, title, body),
            )
        finally:
            prepared.cleanup()

    # -- audit --------------------------------------------------------------

    async def _audit(
        self,
        identity: Identity,
        tool: str,
        args: dict[str, Any],
        run_id: str,
        decision: str,
        rule: str | None,
        started: float,
        backend_status: str | None,
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(timezone.utc).isoformat(),
            run_id=run_id,
            agent_id=identity.agent_id,
            role=identity.role,
            tool=tool,
            args_digest=args_digest(args),
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status=backend_status,
        )
        await asyncio.to_thread(self._audit_store.append, event)
        self._log.info("audit", extra=event.model_dump())
