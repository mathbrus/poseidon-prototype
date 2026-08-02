"""Gateway ASGI app: MCP server (streamable HTTP) + admin endpoint.

Contract C3, full surface: six sandbox tools proxied to the run's registered
tool server, the two git tools (`repo.archive`, `git.open_pr`) backed by
platform-side clones holding the GIT_TOKEN, plus `POST /admin/sandboxes`
(runner only) to register a sandbox address for a run. Bearer auth per
contract C7; every call is policy-checked against OPA and audited (C1/C2).

Run with: uvicorn --factory gateway.app:create_app
"""

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

from gateway.config import GatewaySettings
from gateway.identity import Identity, authenticate, token_map
from gateway.opa import OpaClient
from gateway.service import GatewayService, PolicyDenied


class BearerAuthMiddleware:
    """Reject any HTTP request whose bearer token maps to no known identity."""

    def __init__(self, app: ASGIApp, tokens: dict[str, Identity]) -> None:
        self.app = app
        self.tokens = tokens

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return
        authorization = Headers(scope=scope).get("authorization")
        if authenticate(authorization, self.tokens) is None:
            response = JSONResponse({"detail": "unauthorized"}, status_code=401)
            await response(scope, receive, send)
            return
        await self.app(scope, receive, send)


class RegisterSandboxRequest(BaseModel):
    run_id: str
    url: str


def create_app(
    settings: GatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
) -> Starlette:
    settings = settings or GatewaySettings()
    http_client = http_client or httpx.AsyncClient(timeout=httpx.Timeout(30.0, read=630.0))
    tokens = token_map(settings)
    opa = OpaClient(settings.opa_url, http_client)
    service = GatewayService(settings, opa, http_client)

    mcp = FastMCP(
        "poseidon-gateway",
        stateless_http=True,
        json_response=True,
        # Clients reach the gateway via Docker-network hostnames; auth is the
        # bearer-token middleware, not Host-header checking.
        transport_security=TransportSecuritySettings(enable_dns_rebinding_protection=False),
    )

    def identity_of(ctx: Context) -> Identity:
        request = ctx.request_context.request
        identity = authenticate(request.headers.get("authorization"), tokens)
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

    @mcp.custom_route("/admin/sandboxes", methods=["POST"])
    async def admin_sandboxes(request: Request) -> Response:
        identity = authenticate(request.headers.get("authorization"), tokens)
        if identity is None:  # unreachable behind the middleware
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = RegisterSandboxRequest.model_validate(await request.json())
        except ValidationError as exc:
            return JSONResponse({"detail": exc.errors(include_url=False)}, status_code=422)
        try:
            await service.register_run(identity, body.run_id, body.url)
        except PolicyDenied as exc:
            return JSONResponse({"detail": str(exc), "rule": exc.rule}, status_code=403)
        return JSONResponse({"ok": True})

    app = mcp.streamable_http_app()
    app.add_middleware(BearerAuthMiddleware, tokens=tokens)
    # Exposed for tests (session-manager lifespan) and introspection.
    app.state.fastmcp = mcp
    app.state.service = service
    return app
