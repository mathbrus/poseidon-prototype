"""Gateway enforcement pipeline: policy input -> OPA -> audit -> backend.

Every call (MCP tool or admin endpoint) goes through `GatewayService.call`,
which builds the policy decision input — enriched with the verified run
binding from the registry — queries the caller's compiled
decision path, emits an audit event, and only on allow executes the
backend action.

Run-binding enrichment: the gateway resolves the claimed run against the
registry (live sandbox registration, its namespace, the run row) and injects
`run_binding_verified` into the *input*. Policy never reads pushed data, so
OPA stays a pure function of bundle + input.
"""

import asyncio
import hashlib
import json
import time
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import Any

import httpx

from common.audit import AuditEvent
from common.identity import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PLATFORM,
    Identity,
)
from common.telemetry import get_logger
from gateway.config import GatewaySettings
from gateway.git_backend import GitBackend
from gateway.opa import PLATFORM_DECISION_PATH, OpaClient, company_decision_path
from registry import AuditStore, Runs, SandboxRegistrations

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
        runs: Runs,
        crew_requests: Any = None,
        participants: Any = None,
        ledger: Any = None,
        companies: Any = None,
        git_backend: GitBackend | None = None,
    ) -> None:
        self._settings = settings
        self._opa = opa
        self._http = http_client
        self._git = git_backend or GitBackend(settings, http_client)
        self._log = get_logger("gateway")
        # Registry-backed: an in-memory dict would silently orphan every
        # sandbox call of every in-flight run whenever this process
        # restarted.
        self._sandboxes = sandboxes
        self._audit_store = audit
        # Read-only: the run row supplies the decision input's `workflow`.
        self._runs = runs
        # Written on the crew.request tool path (the tool and the
        # workflow path record the same row).
        self._crew_requests = crew_requests
        # Read-only, for the crew.request enrichment: the
        # conductor's live spawn count and the run's budget headroom ride
        # the decision input — the compiled caps read them there, never
        # from pushed data.
        self._participants = participants
        self._ledger = ledger
        self._companies = companies

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

        policy_input = await self._decision_input(identity, tool, args, run_id)
        if policy_input is None:
            # Parentage is mandatory for ephemeral callers — an
            # identity missing it never reaches policy.
            await self._audit(
                identity, tool, args, run_id, "deny",
                "ephemeral_parentage_missing", started, None,
            )
            raise PolicyDenied("ephemeral_parentage_missing")
        workflow = policy_input["workflow"]
        path = (
            PLATFORM_DECISION_PATH
            if identity.lifecycle == LIFECYCLE_PLATFORM
            else company_decision_path(identity.company or "")
        )
        decision = await self._opa.decide(policy_input, path)

        if not decision.allow:
            await self._audit(
                identity, tool, args, run_id, "deny", decision.rule, started,
                None, workflow,
            )
            raise PolicyDenied(decision.rule)

        try:
            result = await backend()
        except Exception:
            await self._audit(
                identity, tool, args, run_id, "allow", decision.rule, started,
                "error", workflow,
            )
            raise
        await self._audit(
            identity, tool, args, run_id, "allow", decision.rule, started,
            "ok", workflow,
        )
        return result

    async def _decision_input(
        self, identity: Identity, tool: str, args: dict[str, Any], run_id: str
    ) -> dict[str, Any] | None:
        """The policy decision input, run binding verified against the registry.

        Wrong, stale or revoked `run_id` claims resolve to
        `run_binding_verified: false`, and every run-scoped compiled rule
        requires the flag — so they fail closed at the policy layer, exactly
        where `data.runs.assignments` used to catch them.
        """
        registration = run_row = None
        if run_id:
            def read():
                return self._sandboxes.active(run_id), self._runs.get(run_id)

            registration, run_row = await asyncio.to_thread(read)

        if identity.lifecycle == LIFECYCLE_EPHEMERAL:
            # The participants row bound this identity to exactly one run.
            verified = identity.run_id == run_id and bool(run_id)
        elif identity.lifecycle == LIFECYCLE_PLATFORM:
            verified = registration is not None
        else:
            # Standing callers: the run's sandbox must live in the caller's
            # own namespace — a live registration of another company is a
            # cross-tenant claim, not a binding.
            verified = (
                registration is not None
                and identity.namespace is not None
                and registration["namespace"] == identity.namespace
            )

        policy_input = {
            "company": identity.company or "",
            "agent_id": identity.agent_id,
            "lifecycle": identity.lifecycle,
            "parent_agent_id": identity.parent_agent_id or "",
            "role": identity.role,
            "run_id": run_id,
            "workflow": run_row.workflow if run_row else "",
            "tool": tool,
            "args": args,
            "run_binding_verified": verified,
        }
        if identity.lifecycle == LIFECYCLE_EPHEMERAL:
            if not (
                identity.parent_agent_id
                and identity.crew_template
                and identity.crew_role
            ):
                return None
            policy_input["crew_template"] = identity.crew_template
            policy_input["crew_role"] = identity.crew_role
        if tool == "crew.request":
            def spawn_state() -> tuple[int, bool]:
                spawns = self._participants.active_spawn_count(identity.agent_id)
                spec = self._companies.get(identity.company or "") or {}
                cap = float(((spec.get("budgets") or {}).get("per_run_usd")) or 0)
                headroom = cap <= 0 or self._ledger.spent_for_run(run_id) < cap
                return spawns, headroom

            spawns, headroom = await asyncio.to_thread(spawn_state)
            policy_input["active_spawns"] = spawns
            policy_input["budget_headroom"] = headroom
        return policy_input

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
        """Admin action: register a sandbox. The registration row *is* the
        run binding — nothing is pushed to OPA."""

        async def backend() -> None:
            await self.register_sandbox(run_id, url, namespace, job_name)

        await self.call(
            identity, "/admin/sandboxes", {"run_id": run_id, "url": url}, backend
        )

    async def unregister_run(self, identity: Identity, run_id: str) -> None:
        """Admin action: retire a registration at teardown — from here the
        run binding is stale and every run-scoped call for it fails closed."""

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
            # Bound outside the closure on purpose: Python unbinds the
            # `except ... as` name when the block exits, so a closure that
            # captured `exc` itself would work only while the backend is
            # still called inside this `await`.
            failure = BackendError(str(exc))

            async def failing_backend() -> Any:
                raise failure

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

    # -- issues + review + merge ------------------------------------------

    async def issues_create(
        self, identity: Identity, run_id: str, repo: str, title: str, body: str
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "title": title, "body": body}
        return await self.call(
            identity, "issues.create", args,
            lambda: self._git.create_issue(repo, title, body),
        )

    async def issues_read(
        self, identity: Identity, run_id: str, repo: str, number: int | None
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number}
        return await self.call(
            identity, "issues.read", args, lambda: self._git.read_issues(repo, number)
        )

    async def issues_update(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        number: int,
        title: str | None,
        body: str | None,
        state: str | None,
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number,
                "title": title, "body": body, "state": state}
        return await self.call(
            identity, "issues.update", args,
            lambda: self._git.update_issue(repo, number, title, body, state),
        )

    async def issues_comment(
        self, identity: Identity, run_id: str, repo: str, number: int, body: str
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number, "body": body}
        return await self.call(
            identity, "issues.comment", args,
            lambda: self._git.comment_issue(repo, number, body),
        )

    async def git_read(
        self, identity: Identity, run_id: str, repo: str, number: int
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number}
        return await self.call(
            identity, "git.read", args, lambda: self._git.read_pr(repo, number)
        )

    async def git_comment(
        self, identity: Identity, run_id: str, repo: str, number: int, body: str
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number, "body": body}
        return await self.call(
            identity, "git.comment", args,
            lambda: self._git.comment_issue(repo, number, body),
        )

    async def git_merge_pr(
        self, identity: Identity, run_id: str, repo: str, number: int, method: str
    ) -> Any:
        """Policy restricts this to the platform lifecycle — the
        orchestrator calls it after the human merge gate."""
        args = {"run_id": run_id, "repo": repo, "number": number, "method": method}
        return await self.call(
            identity, "git.merge_pr", args,
            lambda: self._git.merge_pr(repo, number, method),
        )

    async def request_crew(
        self, identity: Identity, run_id: str, crew_template: str, reason: str
    ) -> Any:
        """Record spawn intent under the conductor identity.
        An allowed request is authorization + intent, never fulfillment —
        fulfillment is the orchestrator's. The compiled rule already
        narrows the template to the conductor's may_spawn list."""
        args = {"run_id": run_id, "crew_template": crew_template, "reason": reason}

        async def backend() -> Any:
            return await asyncio.to_thread(
                self._crew_requests.record,
                identity.agent_id,
                crew_template,
                run_id,
                reason,
            )

        return await self.call(identity, "crew.request", args, backend)

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
        workflow: str = "",
    ) -> None:
        event = AuditEvent(
            ts=datetime.now(UTC).isoformat(),
            run_id=run_id,
            agent_id=identity.agent_id,
            role=identity.role,
            tool=tool,
            args_digest=args_digest(args),
            decision=decision,
            rule=rule,
            latency_ms=int((time.monotonic() - started) * 1000),
            backend_status=backend_status,
            # The caller's verified attribution rides every event.
            source="gateway",
            company=identity.company or "",
            lifecycle=identity.lifecycle,
            parent_agent_id=identity.parent_agent_id or "",
            workflow=workflow,
        )
        await asyncio.to_thread(self._audit_store.append, event)
        self._log.info("audit", extra=event.model_dump())
