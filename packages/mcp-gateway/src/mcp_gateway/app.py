"""Gateway ASGI app: MCP server (streamable HTTP) + admin endpoint.

The full tool surface: seven sandbox tools proxied to the run's registered
tool server, the git, pull-request and issue tools backed by platform-side
clones and the GitHub API holding the GIT_TOKEN, the crew-request tool, plus
`POST`/`DELETE /admin/sandboxes` (platform lifecycle only) to register and
retire a run's sandbox address. Authentication is workload identity —
projected service-account tokens verified via TokenReview, nothing else;
every call is policy-checked against the caller's compiled decision path
and audited — the admin routes included, which is why the registration
write lives behind the gateway rather than in the orchestrator.

Run with: uvicorn --factory mcp_gateway.app:create_app
"""

import contextlib
from typing import Any

import httpx
from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from pydantic import BaseModel, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, Response

from blueprint.tools import ARCHITECTURE_REQUEST, GIT_CLOSE_PR, ISSUES_CLOSE
from common.authn import Authenticator, AuthMiddleware, Reviewer, default_reviewer
from common.identity import Identity
from mcp_gateway.config import McpGatewaySettings
from mcp_gateway.opa import OpaClient
from mcp_gateway.service import GatewayService, PolicyDenied
from registry import (
    AgentInstances,
    AuditStore,
    Companies,
    FiledTickets,
    RunParticipants,
    Runs,
    SandboxRegistrations,
    open_registry,
)


class RegisterSandboxRequest(BaseModel):
    run_id: str
    url: str
    namespace: str | None = None
    job_name: str | None = None


class UnregisterSandboxRequest(BaseModel):
    run_id: str


def create_app(
    settings: McpGatewaySettings | None = None,
    http_client: httpx.AsyncClient | None = None,
    sandboxes: SandboxRegistrations | None = None,
    audit: AuditStore | None = None,
    instances: AgentInstances | None = None,
    participants: RunParticipants | None = None,
    runs: Runs | None = None,
    companies: Companies | None = None,
    filed: FiledTickets | None = None,
    token_reviewer: Reviewer | None = None,
) -> Starlette:
    settings = settings or McpGatewaySettings()
    http_client = http_client or httpx.AsyncClient(
        timeout=httpx.Timeout(30.0, read=630.0)
    )
    opa = OpaClient(settings.opa_url, http_client)
    # Registry accessors (sandbox registrations and audit events;
    # standing-agent and ephemeral-identity reads for workload identity; run
    # reads for decision-input enrichment). Injectable so
    # tests need no database — all or none: injecting a subset would still
    # open a real pool for the rest.
    accessors = (sandboxes, audit, instances, participants, runs, companies, filed)
    registry = None
    if all(a is None for a in accessors):
        registry = open_registry()
        sandboxes = SandboxRegistrations(registry)
        audit = AuditStore(registry)
        instances = AgentInstances(registry)
        participants = RunParticipants(registry)
        runs = Runs(registry)
        companies = Companies(registry)
        filed = FiledTickets(registry)
    elif any(a is None for a in accessors):
        raise ValueError("inject all registry accessors, or none")
    service = GatewayService(
        settings,
        opa,
        http_client,
        sandboxes,
        audit,
        runs,
        companies,
        filed,
    )
    authenticator = Authenticator(
        settings,
        audit,
        instances,
        participants,
        # The console's own ServiceAccount is known here, for the one read
        # its company's bundle allows it: the documents.
        companies=companies,
        reviewer=token_reviewer or default_reviewer(settings),
    )

    mcp = FastMCP(
        "poseidon-mcp-gateway",
        stateless_http=True,
        json_response=True,
        # Clients reach the gateway via Docker-network hostnames; auth is the
        # bearer-token middleware, not Host-header checking.
        transport_security=TransportSecuritySettings(
            enable_dns_rebinding_protection=False
        ),
    )

    def identity_of(ctx: Context) -> Identity:
        # Authenticated once by the middleware; the identity rides the
        # request state (re-verifying here would re-run a TokenReview per
        # tool call for nothing).
        identity = getattr(ctx.request_context.request.state, "identity", None)
        if identity is None:  # unreachable behind the middleware
            raise PermissionError("unauthorized")
        return identity

    async def sandbox_call(
        ctx: Context, tool: str, args: dict[str, Any]
    ) -> dict[str, Any]:
        identity = identity_of(ctx)
        return await service.call(
            identity, tool, args, lambda: service.proxy_sandbox(tool, args)
        )

    @mcp.tool(name="sandbox.read_file")
    async def sandbox_read_file(
        run_id: str, path: str, ctx: Context, view_range: tuple[int, int] | None = None
    ) -> dict[str, Any]:
        """One file from the run's workspace — all of it, or the 1-based inclusive
        `[start, end]` line range (`-1` for end of file) — with `start_line` and
        `total_lines`."""
        args: dict[str, Any] = {"run_id": run_id, "path": path}
        if view_range is not None:
            args["view_range"] = view_range
        return await sandbox_call(ctx, "sandbox.read_file", args)

    @mcp.tool(name="sandbox.write_file")
    async def sandbox_write_file(
        run_id: str, path: str, content: str, ctx: Context
    ) -> dict[str, Any]:
        """Create or overwrite one file in the run's workspace."""
        return await sandbox_call(
            ctx,
            "sandbox.write_file",
            {"run_id": run_id, "path": path, "content": content},
        )

    @mcp.tool(name="sandbox.edit_file")
    async def sandbox_edit_file(
        run_id: str,
        path: str,
        command: str,
        new_str: str,
        ctx: Context,
        old_str: str | None = None,
        insert_line: int | None = None,
    ) -> dict[str, Any]:
        """Partial edit of an existing file: `str_replace` swaps text that occurs
        exactly once (ambiguous or absent text is a backend error, the file
        untouched); `insert` adds lines after `insert_line` (`0` = top). Returns
        a numbered snippet around the change."""
        args: dict[str, Any] = {
            "run_id": run_id,
            "path": path,
            "command": command,
            "new_str": new_str,
        }
        if old_str is not None:
            args["old_str"] = old_str
        if insert_line is not None:
            args["insert_line"] = insert_line
        return await sandbox_call(ctx, "sandbox.edit_file", args)

    @mcp.tool(name="sandbox.list_dir")
    async def sandbox_list_dir(run_id: str, path: str, ctx: Context) -> dict[str, Any]:
        """List a workspace directory."""
        return await sandbox_call(
            ctx, "sandbox.list_dir", {"run_id": run_id, "path": path}
        )

    @mcp.tool(name="sandbox.exec")
    async def sandbox_exec(
        run_id: str, cmd: str, ctx: Context, timeout_s: float = 60
    ) -> dict[str, Any]:
        """Run a shell command in the sandbox and return its output and exit code.
        The command runs with no credentials, no cluster API, and the egress
        proxy as its only route out."""
        return await sandbox_call(
            ctx, "sandbox.exec", {"run_id": run_id, "cmd": cmd, "timeout_s": timeout_s}
        )

    @mcp.tool(name="sandbox.run_tests")
    async def sandbox_run_tests(
        run_id: str, ctx: Context, paths: list[str] | None = None
    ) -> dict[str, Any]:
        """Run the blueprint's `test_command` and return the result. Agents may
        call it to check their work; the verify gate of record is the
        orchestrator's own separate invocation. With `paths`, the run is scoped
        to those test paths — the platform's contract-verify call, where
        collecting nothing from a named path is a red exit, not a silent pass."""
        # `paths` scopes the run to named test paths — the platform's
        # contract-verify call; the whole-suite run stays the default.
        args: dict[str, Any] = {"run_id": run_id}
        if paths:
            args["paths"] = paths
        return await sandbox_call(ctx, "sandbox.run_tests", args)

    @mcp.tool(name="sandbox.git_bundle")
    async def sandbox_git_bundle(
        run_id: str, branch: str, ctx: Context
    ) -> dict[str, Any]:
        """A git bundle of the named branch's commits since the recorded base ref,
        base64-encoded. How work leaves the sandbox: as verifiable content, not
        as a push."""
        return await sandbox_call(
            ctx, "sandbox.git_bundle", {"run_id": run_id, "branch": branch}
        )

    @mcp.tool(name="repo.archive")
    async def repo_archive(
        run_id: str,
        repo: str,
        ref: str,
        ctx: Context,
        overlay_ref: str | None = None,
        overlay_paths: list[str] | None = None,
    ) -> dict[str, Any]:
        """Clone `repo` at `ref` with the platform token, strip the remote (and
        with it the credentialed URL), and return the workspace as a base64
        tarball. With `overlay_paths`, those paths are mirrored from
        `overlay_ref` over the checkout — removed first, then re-checked-out —
        which is how the platform composes the contract-verify tree. Platform
        lifecycle only: how code enters a sandbox during fulfilment."""
        # The composed tree: `ref`'s code with `overlay_paths` mirrored
        # from `overlay_ref` — how the platform builds the baseline the
        # contract verify runs (head code, the contract tree as it stands
        # on the base ref).
        return await service.archive_repo(
            identity_of(ctx), run_id, repo, ref, overlay_ref, overlay_paths
        )

    @mcp.tool(name="repo.read_file")
    async def repo_read_file(
        run_id: str, repo: str, ref: str, path: str, ctx: Context
    ) -> dict[str, Any]:
        """One file of the repository at `ref`, as the git host holds it —
        `{present, text}`, a missing file being an ordinary answer.
        Platform lifecycle only; the orchestrator reads the company's
        documents with it at `base_ref`, never from a sandbox a run could
        have edited."""
        return await service.read_file(identity_of(ctx), run_id, repo, ref, path)

    @mcp.tool(name="repo.present")
    async def repo_present(
        run_id: str, repo: str, ref: str, paths: list[str], ctx: Context
    ) -> dict[str, Any]:
        """Which of `paths` the repository holds at `ref` — `{present:
        [...]}`, a file or a directory each, an absent one left out.
        Platform lifecycle only; the review workflow asks it of the
        declared contract tree at `base_ref` before composing a run on it."""
        return await service.present_paths(identity_of(ctx), run_id, repo, ref, paths)

    @mcp.tool(name="git.open_pr")
    async def git_open_pr(
        run_id: str,
        repo: str,
        branch: str,
        title: str,
        body: str,
        bundle_b64: str,
        rationale: str,
        ctx: Context,
    ) -> dict[str, Any]:
        """The only path by which code leaves the system. The gateway verifies the
        bundle, applies it to a fresh platform-side clone, extracts the changed
        paths — and which of them already exist on the base ref — and feeds
        them to policy as real arguments (branch protection, `deny_paths`, the
        contract-tree owner rule, the document fences and the write-once
        strategy), then pushes the branch with its own token and opens the
        pull request. The rationale — why the change is complete and correct
        for its task — is recorded with the call in the run's story."""
        return await service.open_pr(
            identity_of(ctx), run_id, repo, branch, title, body, bundle_b64, rationale
        )

    # -- issues, review, merge, crew intent ---------------------------------

    @mcp.tool(name=ARCHITECTURE_REQUEST)
    async def architecture_request(
        run_id: str, rationale: str, proposal: str, ctx: Context
    ) -> dict[str, Any]:
        """Ask for a change to the architecture masterplan. The platform files
        the proposal as a stewardship ticket on the company's repository — the
        caller chooses neither the crew nor the repository — and the steward
        revises the plan on a later tick. One request per run; asking is not
        writing. The rationale is recorded with the call in the run's story."""
        return await service.architecture_request(
            identity_of(ctx), run_id, rationale, proposal
        )

    @mcp.tool(name="issues.create")
    async def issues_create(
        run_id: str, repo: str, title: str, body: str, rationale: str, ctx: Context
    ) -> dict[str, Any]:
        """File a new issue — how the PM turns the next slice of the strategy
        into tickets, and how the platform files rework and bootstrap
        tickets. The rationale — why this ticket, now — is recorded with the
        call in the run's story."""
        return await service.issues_create(
            identity_of(ctx), run_id, repo, title, body, rationale
        )

    @mcp.tool(name="issues.read")
    async def issues_read(
        run_id: str, repo: str, ctx: Context, number: int | None = None
    ) -> dict[str, Any]:
        """Read one issue by number (refusing pull requests — those belong to
        `git.read`), or the open backlog when `number` is omitted. An issue-driven implement run resolves its ticket through this tool under the
        platform identity."""
        return await service.issues_read(identity_of(ctx), run_id, repo, number)

    @mcp.tool(name="issues.comment")
    async def issues_comment(
        run_id: str, repo: str, number: int, body: str, ctx: Context
    ) -> dict[str, Any]:
        """Comment on an issue — how the PM leaves its one-paragraph scoping summary
        on each ticket it files."""
        return await service.issues_comment(identity_of(ctx), run_id, repo, number, body)

    @mcp.tool(name=ISSUES_CLOSE)
    async def issues_close(
        run_id: str, repo: str, number: int, rationale: str, ctx: Context
    ) -> dict[str, Any]:
        """Close an issue as completed. Compiled policy grants this to the
        platform identity alone: the orchestrator closes the tickets a pull
        request fulfilled the moment it learns the pull request merged, and
        no blueprint can hand it to an agent role. The rationale names the
        merge and is recorded with the call in the run's story."""
        return await service.issues_close(
            identity_of(ctx), run_id, repo, number, rationale
        )

    @mcp.tool(name="git.read")
    async def git_read(
        run_id: str, repo: str, number: int, ctx: Context
    ) -> dict[str, Any]:
        """Read a pull request — metadata and diff — for review."""
        return await service.git_read(identity_of(ctx), run_id, repo, number)

    @mcp.tool(name="git.comment")
    async def git_comment(
        run_id: str, repo: str, number: int, body: str, ctx: Context
    ) -> dict[str, Any]:
        """Post a comment on the pull request's conversation; how the reviewer
        publishes findings."""
        return await service.git_comment(identity_of(ctx), run_id, repo, number, body)

    @mcp.tool(name="git.merge_pr")
    async def git_merge_pr(
        run_id: str,
        repo: str,
        number: int,
        sha: str,
        ctx: Context,
        method: str = "merge",
    ) -> dict[str, Any]:
        """Merge the pull request while its head is still `sha`, the commit
        that was reviewed. A head that moved since merges nothing and
        answers `merged: false` with the refusal. Compiled policy grants
        this to the platform identity alone, and the orchestrator calls it
        only after the merge gate resolved to merge. No blueprint can hand
        it to an agent role."""
        return await service.git_merge_pr(
            identity_of(ctx), run_id, repo, number, sha, method
        )

    @mcp.tool(name=GIT_CLOSE_PR)
    async def git_close_pr(
        run_id: str, repo: str, number: int, rationale: str, ctx: Context
    ) -> dict[str, Any]:
        """Close the pull request without merging it. Compiled policy grants
        this to the platform identity alone: the orchestrator closes a pull
        request whose failed review it filed as a rework ticket, and no
        blueprint can hand it to an agent role. The rationale names the
        ticket and is recorded with the call in the run's story."""
        return await service.git_close_pr(
            identity_of(ctx), run_id, repo, number, rationale
        )

    @mcp.custom_route("/admin/sandboxes", methods=["POST"])
    async def admin_sandboxes(request: Request) -> Response:
        identity = getattr(request.state, "identity", None)
        if identity is None:  # unreachable behind the middleware
            return JSONResponse({"detail": "unauthorized"}, status_code=401)
        try:
            body = RegisterSandboxRequest.model_validate(await request.json())
        except ValidationError as exc:
            return JSONResponse(
                {"detail": exc.errors(include_url=False)}, status_code=422
            )
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
            return JSONResponse(
                {"detail": exc.errors(include_url=False)}, status_code=422
            )
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
