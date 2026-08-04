"""Gateway ASGI app: MCP server (streamable HTTP) + admin endpoint.

Contract C3, full surface: six sandbox tools proxied to the run's registered
tool server, the two git tools (`repo.archive`, `git.open_pr`) backed by
platform-side clones holding the GIT_TOKEN, plus `POST`/`DELETE
/admin/sandboxes` (platform lifecycle only) to register and retire a run's
sandbox address. Authentication is workload identity (projected
service-account tokens via TokenReview, platform-signed ephemeral JWTs —
plan step 11; the static PoC tokens died at the step-12 cutover); every
call is policy-checked against the caller's compiled decision path and
audited (C1/C2) — the admin routes included, which is why the registration
write lives behind the gateway rather than in the orchestrator.

Run with: uvicorn --factory gateway.app:create_app
"""

import contextlib
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.datastructures import Headers
from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.types import ASGIApp, Receive, Scope, Send

from common.authn import Authenticator, Reviewer, default_reviewer
from gateway.config import GatewaySettings
from common.identity import Identity
from gateway.opa import OpaClient
from gateway.service import GatewayService, PolicyDenied
from registry import (
    AuditStore,
    BudgetLedger,
    CrewRequests,
    RunParticipants,
    Runs,
    SandboxRegistrations,
    open_registry,
)
from registry.bundles import AgentInstances, Companies


class AuthMiddleware:
    """Authenticate every HTTP request once (platform JWT or Kubernetes
    TokenReview — plan step 11) and stash the identity in the request
    state; rejected tokens are audited by the authenticator."""

    def __init__(self, app: ASGIApp, authenticator: Authenticator) -> None:
        self.app = app
        self.authenticator = authenticator

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization")
        identity = await self.authenticator.authenticate(
            authorization, scope.get("path", "")
        )
        if identity is None:
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        scope.setdefault("state", {})["identity"] = identity
        await self.app(scope, receive, send)


class RegisterSandboxRequest(BaseModel):
    run_id: str
    url: str
    namespace: str | None = None
    job_name: str | None = None


class UnregisterSandboxRequest(BaseModel):
    run_id: str


def create_app(
    settings: GatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    sandboxes: SandboxRegistrations | None = None,
    audit: AuditStore | None = None,
    instances: AgentInstances | None = None,
    participants: RunParticipants | None = None,
    runs: Runs | None = None,
    crew_requests: CrewRequests | None = None,
    ledger: BudgetLedger | None = None,
    companies: Companies | None = None,
    token_reviewer: Reviewer | None = None,
) -> Starlette:
    settings = settings or GatewaySettings()
    http_client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=630.0))
    opa = OpaClient(settings.opa_url, http_client)
    # Registry accessors (sandbox registrations, audit events since step 8;
    # instance/participant reads for workload identity since step 11; run
    # reads for decision-input enrichment since step 12). Injectable so
    # tests need no database — all or none: injecting a subset would still
    # open a real pool for the rest.
    accessors = (
        sandboxes, audit, instances, participants, runs, crew_requests,
        ledger, companies,
    )
    registry = None
    if all(a is None for a in accessors):
        registry = open_registry()
        sandboxes = SandboxRegistrations(registry)
        audit = AuditStore(registry)
        instances = AgentInstances(registry)
        participants = RunParticipants(registry)
        runs = Runs(registry)
        crew_requests = CrewRequests(registry)
        ledger = BudgetLedger(registry)
        companies = Companies(registry)
    elif any(a is None for a in accessors):
        raise ValueError("inject all registry accessors, or none")
    service = GatewayService(
        settings, opa, http_client, sandboxes, audit, runs, crew_requests,
        participants, ledger, companies,
    )
    authenticator = Authenticator(
        settings,
        audit,
        instances,
        participants,
        reviewer=token_reviewer or default_reviewer(settings),
    )

    mcp = FastMCP(
        "poseidon-gateway",
        stateless_http=True,
        json_response=True,
        # Clients reach the gateway via Docker-network hostnames; auth is the
        # bearer-token middleware, not Host-header checking.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def identity_of(ctx: Context) -> Identity:
        # Authenticated once by the middleware; the identity rides the
        # request state (re-verifying here would re-run a TokenReview per
        # tool call for nothing).
        identity = getattr(ctx.request_context.request.state, "identity", None)
        if identity is None:  # unreachable behind the middleware
            raise PermissionError("unauthorized")
        return identity

    async def sandbox_call(ctx: Context, tool: str, args: dict[str, Any]) -> dict[str, Any]:
        identity = identity_of(ctx)
        return await service.call(
            identity, tool, args, lambda: service.proxy_sandbox(tool, args)
        )

    @mcp.tool(name="sandbox.read_file")
    async def sandbox_read_file(run_id: str, path: str, ctx: Context) -> dict[str, Any]:
        return await sandbox_call(ctx, "sandbox.read_file", {"run_id": run_id, "path": path})

    @mcp.tool(name="sandbox.write_file")
    async def sandbox_write_file(
        run_id: str, path: str, content: str, ctx: Context
    ) -> dict[str, Any]:
        return await sandbox_call(
            ctx, "sandbox.write_file", {"run_id": run_id, "path": path, "content": content}
        )

    @mcp.tool(name="sandbox.list_dir")
    async def sandbox_list_dir(run_id: str, path: str, ctx: Context) -> dict[str, Any]:
        return await sandbox_call(ctx, "sandbox.list_dir", {"run_id": run_id, "path": path})

    @mcp.tool(name="sandbox.exec")
    async def sandbox_exec(
        run_id: str, cmd: str, ctx: Context, timeout_s: float = 60
    ) -> dict[str, Any]:
        return await sandbox_call(
            ctx, "sandbox.exec", {"run_id": run_id, "cmd": cmd, "timeout_s": timeout_s}
        )

    @mcp.tool(name="sandbox.run_tests")
    async def sandbox_run_tests(run_id: str, ctx: Context) -> dict[str, Any]:
        return await sandbox_call(ctx, "sandbox.run_tests", {"run_id": run_id})

    @mcp.tool(name="sandbox.git_bundle")
    async def sandbox_git_bundle(run_id: str, branch: str, ctx: Context) -> dict[str, Any]:
        return await sandbox_call(
            ctx, "sandbox.git_bundle", {"run_id": run_id, "branch": branch}
        )

    @mcp.tool(name="repo.archive")
    async def repo_archive(
        run_id: str, repo: str, ref: str, ctx: Context
    ) -> dict[str, Any]:
        return await service.archive_repo(identity_of(ctx), run_id, repo, ref)

    @mcp.tool(name="git.open_pr")
    async def git_open_pr(
        run_id: str,
        repo: str,
        branch: str,
        title: str,
        body: str,
        bundle_b64: str,
        ctx: Context,
    ) -> dict[str, Any]:
        return await service.open_pr(
            identity_of(ctx), run_id, repo, branch, title, body, bundle_b64
        )

    # -- plan step 15: issues, review, merge, crew intent -------------------

    @mcp.tool(name="issues.create")
    async def issues_create(
        run_id: str, repo: str, title: str, body: str, ctx: Context
    ) -> dict[str, Any]:
        return await service.issues_create(identity_of(ctx), run_id, repo, title, body)

    @mcp.tool(name="issues.read")
    async def issues_read(
        run_id: str, repo: str, ctx: Context, number: int | None = None
    ) -> dict[str, Any]:
        return await service.issues_read(identity_of(ctx), run_id, repo, number)

    @mcp.tool(name="issues.update")
    async def issues_update(
        run_id: str,
        repo: str,
        number: int,
        ctx: Context,
        title: str | None = None,
        body: str | None = None,
        state: str | None = None,
    ) -> dict[str, Any]:
        return await service.issues_update(
            identity_of(ctx), run_id, repo, number, title, body, state
        )

    @mcp.tool(name="issues.comment")
    async def issues_comment(
        run_id: str, repo: str, number: int, body: str, ctx: Context
    ) -> dict[str, Any]:
        return await service.issues_comment(identity_of(ctx), run_id, repo, number, body)

    @mcp.tool(name="git.read")
    async def git_read(run_id: str, repo: str, number: int, ctx: Context) -> dict[str, Any]:
        return await service.git_read(identity_of(ctx), run_id, repo, number)

    @mcp.tool(name="git.comment")
    async def git_comment(
        run_id: str, repo: str, number: int, body: str, ctx: Context
    ) -> dict[str, Any]:
        return await service.git_comment(identity_of(ctx), run_id, repo, number, body)

    @mcp.tool(name="git.merge_pr")
    async def git_merge_pr(
        run_id: str, repo: str, number: int, ctx: Context, method: str = "merge"
    ) -> dict[str, Any]:
        return await service.git_merge_pr(
            identity_of(ctx), run_id, repo, number, method
        )

    @mcp.tool(name="crew.request")
    async def crew_request(
        run_id: str, crew_template: str, reason: str, ctx: Context
    ) -> dict[str, Any]:
        return await service.request_crew(
            identity_of(ctx), run_id, crew_template, reason
        )

    @mcp.custom_route("/admin/sandboxes", methods=["POST"])
    async def admin_sandboxes(request: Request) -> Response:
        identity = getattr(request.state, "identity", None)
        if identity is None:  # unreachable behind the middleware
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = RegisterSandboxRequest.model_validate(await request.json())
        except ValidationError as exc:
            return JSONResponse({"detail": exc.errors(include_url=False)}, status_code=422)
        try:
            await service.register_run(
                identity, body.run_id, body.url, body.namespace, body.job_name
            )
        except PolicyDenied as exc:
            return JSONResponse({"detail": str(exc), "rule": exc.rule}, status_code=403)
        return JSONResponse({"ok": True})

    @mcp.custom_route("/admin/sandboxes", methods=["DELETE"])
    async def admin_sandboxes_delete(request: Request) -> Response:
        identity = getattr(request.state, "identity", None)
        if identity is None:  # unreachable behind the middleware
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = UnregisterSandboxRequest.model_validate(await request.json())
        except ValidationError as exc:
            return JSONResponse({"detail": exc.errors(include_url=False)}, status_code=422)
        try:
            await service.unregister_run(identity, body.run_id)
        except PolicyDenied as exc:
            return JSONResponse({"detail": str(exc), "rule": exc.rule}, status_code=403)
        return JSONResponse({"ok": True})

    app = mcp.streamable_http_app()
    app.add_middleware(AuthMiddleware, authenticator=authenticator)
    if registry is not None:
        # Close the self-opened pool on shutdown by wrapping the session
        # manager's lifespan (this Starlette has no on_shutdown events API).
        inner_lifespan = app.router.lifespan_context

        @contextlib.asynccontextmanager
        async def lifespan_with_registry(started_app):
            async with inner_lifespan(started_app):
                try:
                    yield
                finally:
                    registry.close()

        app.router.lifespan_context = lifespan_with_registry
    # Exposed for tests (session-manager lifespan) and introspection.
    app.state.fastmcp = mcp
    app.state.service = service
    return app
