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
import time
from collections.abc import Awaitable, Callable
from typing import Any

import httpx

from blueprint.catalog import STEWARD_ROLE, STEWARDSHIP_CREW
from blueprint.documents import DOCUMENT_LABELS
from blueprint.tools import (
    ARCHITECTURE_REQUEST,
    GIT_CLOSE_PR,
    ISSUES_CLOSE,
    ISSUES_DISMISS,
    RATIONALE_TOOLS,
    REPO_PURGE,
)
from common.audit import RATIONALE_LIMIT, AuditEvent, args_digest
from common.documents import BOOTSTRAP_TITLES
from common.identity import (
    LIFECYCLE_EPHEMERAL,
    LIFECYCLE_PLATFORM,
    Identity,
)
from common.policy import (
    GIT_OPEN_PR_ARCHITECTURE_PATH,
    GIT_OPEN_PR_BRANCH_PATTERN,
    GIT_OPEN_PR_CONTRACT_TREE,
    GIT_OPEN_PR_DENIED_PATHS,
    GIT_OPEN_PR_PROTECTED_BRANCH,
    GIT_OPEN_PR_STRATEGY_PATH,
    GIT_OPEN_PR_STRATEGY_PRESENT,
    PLATFORM_DECISION_PATH,
    REPO_NOT_DECLARED,
    company_decision_path,
)
from common.tasks import CLOSE_NOT_PLANNED, crew_trailer
from common.telemetry import get_logger
from common.workflows import RUN_STATUS_RUNNING
from mcp_gateway.config import McpGatewaySettings
from mcp_gateway.git_backend import GitBackend
from mcp_gateway.opa import OpaClient
from registry import (
    AuditStore,
    Companies,
    FiledTickets,
    Runs,
    SandboxRegistrations,
)

# tool name -> sandbox tool-server route
SANDBOX_ROUTES = {
    "sandbox.read_file": "/files/read",
    "sandbox.write_file": "/files/write",
    "sandbox.edit_file": "/files/edit",
    "sandbox.list_dir": "/files/list",
    "sandbox.exec": "/exec",
    "sandbox.run_tests": "/tests",
    "sandbox.git_bundle": "/bundle",
}


class PolicyDenied(Exception):
    """A call policy refused: the rule that fired — what the audit row
    carries — and, when the gateway can say it from the call's arguments
    and the applied blueprint, what the rule refused: the branch, the
    path. The caller reads the text as its tool observation and complies;
    a bare rule name told it only that it was wrong."""

    def __init__(self, rule: str | None, detail: str | None = None) -> None:
        self.rule = rule or "unknown"
        self.detail = detail
        message = f"denied by policy rule: {self.rule}"
        super().__init__(f"{message} — {detail}" if detail else message)


def _under(paths: list[str], prefixes: list[str]) -> list[str]:
    return [p for p in paths if any(p.startswith(prefix) for prefix in prefixes)]


def _explain_repo(args: dict[str, Any], spec: dict[str, Any]) -> str:
    return f"the company's repository is {spec['repo']['url']}, not {args.get('repo')!r}"


def _explain_protected(args: dict[str, Any], spec: dict[str, Any]) -> str:
    pattern = (spec["policies"].get("branch_protection") or {}).get("allowed_pattern")
    text = f"{args.get('branch')} is a protected branch"
    return f"{text}; a pull request's branch must match {pattern}" if pattern else text


def _explain_pattern(args: dict[str, Any], spec: dict[str, Any]) -> str:
    pattern = (spec["policies"].get("branch_protection") or {}).get("allowed_pattern")
    return f"branch {args.get('branch')!r} does not match {pattern}"


def _explain_denied_paths(args: dict[str, Any], spec: dict[str, Any]) -> str:
    prefixes = sorted(spec["policies"].get("deny_paths") or [])
    hit = _under(list(args.get("paths") or []), prefixes)
    return f"no pull request may change {', '.join(hit)} (under {', '.join(prefixes)})"


def _explain_contract_tree(args: dict[str, Any], spec: dict[str, Any]) -> str:
    contract = spec["policies"].get("contract_tests") or {}
    hit = _under(list(args.get("paths") or []), sorted(contract.get("paths") or []))
    return (
        f"{', '.join(hit)} is in the contract tree, which only "
        f"{', '.join(sorted(contract.get('roles') or []))} delivers into"
    )


def _explain_document(name: str):
    def explain(args: dict[str, Any], spec: dict[str, Any]) -> str:
        return (
            f"{spec['documents'][name]} is the {DOCUMENT_LABELS[name]}; only the "
            f"{STEWARD_ROLE} delivers into it"
        )

    return explain


def _explain_strategy_present(args: dict[str, Any], spec: dict[str, Any]) -> str:
    return (
        f"{spec['documents']['strategy']} is the strategy and already exists on "
        f"{spec['repo']['base_ref']}; it is written once"
    )


# Rule -> what it refused, read from the call's arguments and the applied
# spec. A rule not listed here is explained by its name alone.
_EXPLAINERS = {
    REPO_NOT_DECLARED: _explain_repo,
    GIT_OPEN_PR_PROTECTED_BRANCH: _explain_protected,
    GIT_OPEN_PR_BRANCH_PATTERN: _explain_pattern,
    GIT_OPEN_PR_DENIED_PATHS: _explain_denied_paths,
    GIT_OPEN_PR_CONTRACT_TREE: _explain_contract_tree,
    GIT_OPEN_PR_ARCHITECTURE_PATH: _explain_document("architecture"),
    GIT_OPEN_PR_STRATEGY_PATH: _explain_document("strategy"),
    GIT_OPEN_PR_STRATEGY_PRESENT: _explain_strategy_present,
}


def explain_denial(
    rule: str | None, args: dict[str, Any], spec: dict[str, Any] | None
) -> str | None:
    """What `rule` refused in this call, in the words of the applied
    blueprint — the branch and the pattern it had to match, the path and
    the prefix it fell under — or None when the rule has no explainer or
    no company is applied (a platform caller, a torn-down company)."""
    explainer = _EXPLAINERS.get(rule or "")
    if explainer is None or not spec:
        return None
    return explainer(args, spec)


# The one tool whose backend files a ticket the caller does not shape.
# The title of the stewardship ticket the platform files for an
# architecture request; the run id follows, so the requesting run's
# record and the ticket name each other.
ARCHITECTURE_REQUEST_TITLE_PREFIX = "Architecture request from run "


def stated_rationale(tool: str, args: dict[str, Any]) -> str | None:
    """The caller's reason for a call that commits the company to
    something (`RATIONALE_TOOLS`), bounded, or None for every other tool.
    A committing call without one is malformed — refused before policy is
    asked, like any missing argument — and the refusal says what is owed."""
    if tool not in RATIONALE_TOOLS:
        return None
    stated = str(args.get("rationale") or "").strip()
    if not stated:
        raise ValueError(
            f"{tool} owes a rationale: say, in a few sentences, why — it is "
            "recorded with the call in the run's story"
        )
    return stated[:RATIONALE_LIMIT]


class BackendError(Exception):
    pass


class GatewayService:
    def __init__(
        self,
        settings: McpGatewaySettings,
        opa: OpaClient,
        http_client: httpx.AsyncClient,
        sandboxes: SandboxRegistrations,
        audit: AuditStore,
        runs: Runs,
        companies: Companies,
        filed: FiledTickets,
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
        self._companies = companies
        # Every ticket this gateway creates is recorded here the moment the
        # git host answers: the loop reads its own writes while the host's
        # listing catches up.
        self._filed = filed

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
        rationale = stated_rationale(tool, args)
        started = time.monotonic()

        policy_input = await self._decision_input(identity, tool, args, run_id)
        if policy_input is None:
            # Parentage is mandatory for ephemeral callers — an
            # identity missing it never reaches policy.
            await self._audit(
                identity,
                tool,
                args,
                run_id,
                "deny",
                "ephemeral_parentage_missing",
                started,
                None,
                rationale=rationale,
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
                identity,
                tool,
                args,
                run_id,
                "deny",
                decision.rule,
                started,
                None,
                workflow,
                rationale=rationale,
            )
            # The row keeps the bare rule; the caller gets the rule and
            # what it refused, read from the applied blueprint — the same
            # record the bundle that refused it was compiled from.
            spec = await asyncio.to_thread(self._companies.get, identity.company or "")
            raise PolicyDenied(decision.rule, explain_denial(decision.rule, args, spec))

        try:
            result = await backend()
        except Exception:
            await self._audit(
                identity,
                tool,
                args,
                run_id,
                "allow",
                decision.rule,
                started,
                "error",
                workflow,
                rationale=rationale,
            )
            raise
        await self._audit(
            identity,
            tool,
            args,
            run_id,
            "allow",
            decision.rule,
            started,
            "ok",
            workflow,
            rationale=rationale,
        )
        return result

    async def _decision_input(
        self, identity: Identity, tool: str, args: dict[str, Any], run_id: str
    ) -> dict[str, Any] | None:
        """The policy decision input, run binding verified against the registry.

        Wrong, stale or revoked `run_id` claims resolve to
        `run_binding_verified: false`, and every run-scoped compiled rule
        requires the flag — so they fail closed at the policy layer.
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
            # own namespace — a live registration anywhere else is a claim
            # on a run that is not the caller's, not a binding.
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
            if not (identity.parent_agent_id and identity.crew_template):
                return None
            policy_input["crew_template"] = identity.crew_template
        if tool == ARCHITECTURE_REQUEST:
            # The per-run cap reads how many requests this run already
            # filed — allowed and answered — off the gateway's own audit
            # trail, the one record it writes, so the fact needs no new
            # store. A request the host failed to file does not count:
            # the model may ask again.
            policy_input["architecture_requests"] = await asyncio.to_thread(
                self._audit_store.count_allowed, run_id, ARCHITECTURE_REQUEST
            )
        if tool == ISSUES_DISMISS:
            policy_input.update(await self._dismiss_facts(identity, args, run_row))
        return policy_input

    async def _dismiss_facts(
        self, identity: Identity, args: dict[str, Any], run_row: Any
    ) -> dict[str, Any]:
        """What the compiled dismissal limits read: whether the claimed run
        is a live run of the caller's company (`run_live`, beside the
        input's `workflow`), how many dismissals it already made, off the
        gateway's own audit trail (`dismissals`), and the ticket as the git
        host and the registry know it (`dismiss_target`: open, filed for an
        absent document, held by a live run or a standing delivery). The
        ticket is read only on the company's own repository; one that
        cannot be read leaves its facts out, and an absent fact denies."""
        company = identity.company or ""
        run_id = str(args.get("run_id") or "")
        facts: dict[str, Any] = {
            "run_live": bool(
                run_row is not None
                and run_row.status == RUN_STATUS_RUNNING
                and run_row.company == company
            ),
            "dismissals": await asyncio.to_thread(
                self._audit_store.count_allowed, run_id, ISSUES_DISMISS
            ),
        }
        spec = await asyncio.to_thread(self._companies.get, company) or {}
        repo = str(args.get("repo") or "")
        if repo != (spec.get("repo") or {}).get("url"):
            return facts
        try:
            number = int(args["number"])
            (issue,) = (await self._git.read_issues(repo, number))["issues"]
        except Exception as exc:  # an unreadable ticket is a fact left out
            self._log.info("dismissal target unreadable", extra={"error": str(exc)})
            return facts
        attempts = (await asyncio.to_thread(self._runs.issue_attempts, company)).get(
            number
        )
        facts["dismiss_target"] = {
            "open": issue.get("state") == "open",
            "bootstrap": issue.get("title") in BOOTSTRAP_TITLES.values(),
            "in_flight": bool(attempts and (attempts.live or attempts.concluded)),
        }
        return facts

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

        await self.call(identity, "/admin/sandboxes.delete", {"run_id": run_id}, backend)

    # -- git tools ----------------------------------------------------------

    async def archive_repo(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        ref: str,
        overlay_ref: str | None = None,
        overlay_paths: list[str] | None = None,
        merge_onto: str | None = None,
    ) -> Any:
        # run_id rides the args so the workspace fetch is attributable like
        # every other step: without it this event is the one hole in "the
        # whole story for run X". The overlay composition and the merge
        # are part of the audited args for the same reason: which tree was
        # built is part of what happened.
        args: dict[str, Any] = {"run_id": run_id, "repo": repo, "ref": ref}
        if overlay_ref:
            args["overlay_ref"] = overlay_ref
            args["overlay_paths"] = overlay_paths or []
        if merge_onto:
            args["merge_onto"] = merge_onto
        return await self.call(
            identity,
            "repo.archive",
            args,
            lambda: self._git.archive(repo, ref, overlay_ref, overlay_paths, merge_onto),
        )

    async def read_file(
        self, identity: Identity, run_id: str, repo: str, ref: str, path: str
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "ref": ref, "path": path}
        return await self.call(
            identity, "repo.read_file", args, lambda: self._git.read_file(repo, ref, path)
        )

    async def purge_repo(
        self, identity: Identity, run_id: str, repo: str, base_ref: str
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "base_ref": base_ref}
        return await self.call(
            identity, REPO_PURGE, args, lambda: self._git.purge(repo, base_ref)
        )

    async def present_paths(
        self, identity: Identity, run_id: str, repo: str, ref: str, paths: list[str]
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "ref": ref, "paths": paths}
        return await self.call(
            identity,
            "repo.present",
            args,
            lambda: self._git.present_paths(repo, ref, paths),
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
        rationale: str,
    ) -> Any:
        """Verify + apply the bundle, then authorize with the changed paths.

        The bundle is applied to a platform-side clone *before* the policy
        query so the changed paths can be part of the policy input (`args.paths`).
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
            "rationale": rationale,
        }
        try:
            # The branch the change is measured and delivered against is the
            # caller's company's, from its applied blueprint.
            spec = await asyncio.to_thread(self._companies.get, identity.company or "")
            if not spec:
                raise BackendError(
                    f"no applied blueprint for company {identity.company!r}"
                )
            base_ref = spec["repo"]["base_ref"]
            prepared = await self._git.prepare_pr(repo, branch, bundle_b64, base_ref)
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
                {
                    **args,
                    "paths": prepared.paths,
                    "paths_present": prepared.paths_present,
                },
                lambda: self._git.open_pr(prepared, repo, title, body, base_ref),
            )
        finally:
            prepared.cleanup()

    # -- issues + review + merge ------------------------------------------

    async def architecture_request(
        self, identity: Identity, run_id: str, rationale: str, proposal: str
    ) -> Any:
        """Record an ephemeral agent's proposal for the architecture masterplan
        as a stewardship ticket, filed by the platform on the company's
        repository: the caller chooses neither the crew nor the repository,
        and the ticket is the record — the audit event its receipt."""
        args = {"run_id": run_id, "rationale": rationale, "proposal": proposal}

        async def backend() -> Any:
            spec = await asyncio.to_thread(self._companies.get, identity.company or "")
            if not spec:
                raise BackendError(
                    f"no applied blueprint for company {identity.company!r}"
                )
            headline = (proposal.strip().splitlines() or ["(no proposal)"])[0][:80]
            title = f"{ARCHITECTURE_REQUEST_TITLE_PREFIX}{run_id}: {headline}"
            body = (
                f"Rationale:\n{rationale.strip()}\n\n"
                f"Proposal:\n{proposal.strip()}\n\n"
                "Revise the architecture masterplan accordingly, or decline "
                "in the pull request and say why.\n\n"
                f"Filed by the platform for run {run_id} at the request of "
                f"{identity.agent_id}.\n\n"
                f"{crew_trailer(STEWARDSHIP_CREW)}"
            )
            return await self._file_ticket(spec["repo"]["url"], run_id, title, body)

        return await self.call(identity, ARCHITECTURE_REQUEST, args, backend)

    async def _file_ticket(self, repo: str, run_id: str, title: str, body: str) -> Any:
        """Create the issue and remember it under the run that filed it."""
        issue = await self._git.create_issue(repo, title, body)
        await asyncio.to_thread(self._filed.record, repo, int(issue["number"]), run_id)
        return issue

    async def issues_create(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        title: str,
        body: str,
        rationale: str,
    ) -> Any:
        args = {
            "run_id": run_id,
            "repo": repo,
            "title": title,
            "body": body,
            "rationale": rationale,
        }
        return await self.call(
            identity,
            "issues.create",
            args,
            lambda: self._file_ticket(repo, run_id, title, body),
        )

    async def issues_read(
        self, identity: Identity, run_id: str, repo: str, number: int | None
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number}
        return await self.call(
            identity, "issues.read", args, lambda: self._git.read_issues(repo, number)
        )

    async def issues_comment(
        self, identity: Identity, run_id: str, repo: str, number: int, body: str
    ) -> Any:
        args = {"run_id": run_id, "repo": repo, "number": number, "body": body}
        return await self.call(
            identity,
            "issues.comment",
            args,
            lambda: self._git.comment_issue(repo, number, body),
        )

    async def issues_close(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        number: int,
        reason: str,
        rationale: str,
    ) -> Any:
        """Policy restricts this to the platform lifecycle — the
        orchestrator closes the tickets a merged pull request fulfils and
        the ones the company will not work."""
        args = {
            "run_id": run_id,
            "repo": repo,
            "number": number,
            "reason": reason,
            "rationale": rationale,
        }
        return await self.call(
            identity,
            ISSUES_CLOSE,
            args,
            lambda: self._git.close_issue(repo, number, reason, rationale),
        )

    async def issues_dismiss(
        self, identity: Identity, run_id: str, repo: str, number: int, rationale: str
    ) -> Any:
        """The PM closes, while it scopes, a ticket the company should not
        work: as not planned, its rationale posted on it. The limits are
        the compiled policy's, on the facts `_dismiss_facts` reads."""
        args = {"run_id": run_id, "repo": repo, "number": number, "rationale": rationale}
        return await self.call(
            identity,
            ISSUES_DISMISS,
            args,
            lambda: self._git.close_issue(repo, number, CLOSE_NOT_PLANNED, rationale),
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
            identity,
            "git.comment",
            args,
            lambda: self._git.comment_issue(repo, number, body),
        )

    async def git_merge_pr(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        number: int,
        sha: str,
        method: str,
    ) -> Any:
        """Policy restricts this to the platform lifecycle — the
        orchestrator calls it after the merge gate resolved to merge."""
        args = {
            "run_id": run_id,
            "repo": repo,
            "number": number,
            "sha": sha,
            "method": method,
        }
        return await self.call(
            identity,
            "git.merge_pr",
            args,
            lambda: self._git.merge_pr(repo, number, sha, method),
        )

    async def git_close_pr(
        self,
        identity: Identity,
        run_id: str,
        repo: str,
        number: int,
        rationale: str,
        delete_branch: bool,
    ) -> Any:
        """Policy restricts this to the platform lifecycle — the
        orchestrator closes a pull request its rework ticket supersedes, and
        one whose delivery it refuses."""
        args = {
            "run_id": run_id,
            "repo": repo,
            "number": number,
            "rationale": rationale,
            "delete_branch": delete_branch,
        }
        return await self.call(
            identity,
            GIT_CLOSE_PR,
            args,
            lambda: self._git.close_pr(repo, number, rationale, delete_branch),
        )

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
        rationale: str | None = None,
    ) -> None:
        event = AuditEvent(
            run_id=run_id,
            agent_id=identity.agent_id,
            role=identity.role,
            tool=tool,
            args_digest=args_digest(args),
            decision=decision,
            rule=rule,
            rationale=rationale,
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
