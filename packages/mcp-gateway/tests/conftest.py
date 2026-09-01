"""Gateway unit-test fixtures: stubbed OPA, stubbed sandbox and stubbed
GitHub API behind one httpx.MockTransport, the real MCP app served over an
in-process ASGI transport, and a local bare git repo standing in for
TARGET_REPO (real git subprocesses, no network).

The OPA stub mirrors the *compiled* policy shape:
per-company and platform decision paths, role rules keyed on
lifecycle+company+role, run binding read from the enriched input — and the
test tokens resolve through a fake TokenReview (workload identity)."""

import base64
import json
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from common.authn import ServiceAccount
from mcp_gateway.app import create_app
from mcp_gateway.config import McpGatewaySettings
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryCrewRequests,
    InMemoryRunParticipants,
    InMemoryRuns,
    InMemorySandboxRegistrations,
)

AGENT_TOKEN = "agent-secret"
RUNNER_TOKEN = "runner-secret"
PM_TOKEN = "pm-secret"
REVIEWER_TOKEN = "reviewer-secret"
GIT_TOKEN = "git-platform-secret"
OPA_URL = "http://opa.test"
SANDBOX_URL = "http://sbx.test:8000"
GITHUB_API_URL = "http://github.test"
PR_URL = "https://github.test/example/pull/1"
FEATURE_BRANCH_RE = re.compile(r"^feature/[a-z0-9-]+$")

COMPANY = "hello-saas"
COMPANY_NS = "hello-saas"
PLATFORM_NS = "poseidon-platform"
AGENT_ID = "backend-engineer-1"
COMPANY_DECISION = "/v1/data/poseidon/authz/hello_saas/decision"
PLATFORM_DECISION = "/v1/data/poseidon/platform/decision"


class FakeReviewer:
    """TokenReview double: the test tokens resolve to the same
    ServiceAccounts the kind cluster would report."""

    accounts = {
        AGENT_TOKEN: ServiceAccount(COMPANY_NS, AGENT_ID),
        RUNNER_TOKEN: ServiceAccount(PLATFORM_NS, "orchestrator-worker"),
        PM_TOKEN: ServiceAccount(COMPANY_NS, "pm-1"),
        REVIEWER_TOKEN: ServiceAccount(COMPANY_NS, "ephemeral-reviewer-01x"),
    }

    async def review(self, token: str, audience: str) -> ServiceAccount | None:
        return self.accounts.get(token)


SANDBOX_RESPONSES = {
    "/files/read": {"content": "hello from sandbox"},
    "/files/write": {"ok": True},
    "/files/edit": {"ok": True, "snippet": "     1\tx"},
    "/files/list": {"entries": [{"name": "README.md", "type": "file"}]},
    "/exec": {"exit_code": 0, "stdout": "ran", "stderr": ""},
    "/tests": {"exit_code": 0, "stdout": "1 passed", "stderr": ""},
    "/bundle": {"bundle_b64": "YnVuZGxl", "base_ref": "main"},
}


class Backends:
    """Scriptable stand-ins for OPA, the sandbox tool server and GitHub.

    The OPA stub mirrors the compiled-bundle shape (compiler/rego.py):
    company callers query their company package (standing role rules with
    `run_binding_verified` for run-scoped tools, the blueprint's branch and
    path denies), the platform lifecycle queries its own package. `decide`
    reads the *enriched* input, exactly like the real bundles. `force_allow`
    short-circuits it for tests that need an allow despite no registration.
    """

    def __init__(self) -> None:
        self.sandbox_calls: list[tuple[str, dict]] = []
        self.policy_inputs: list[dict] = []
        self.decision_paths: list[str] = []
        self.github_calls: list[tuple[str, dict, dict]] = []
        self.force_allow = False
        self.sandbox_error: int | None = None
        self.github_error: int | None = None

    def decide(self, policy_input: dict, path: str) -> dict:
        if self.force_allow:
            return {"allow": True, "rule": "forced_allow"}
        tool = policy_input["tool"]
        args = policy_input.get("args", {})
        if path == PLATFORM_DECISION:
            if policy_input.get("lifecycle") != "platform":
                return {"allow": False, "rule": "default_deny"}
            if tool in (
                "repo.archive",
                "/admin/sandboxes",
                "/admin/sandboxes.delete",
                "sandbox.run_tests",
                "git.merge_pr",
            ):
                return {"allow": True, "rule": f"platform_{tool}"}
            return {"allow": False, "rule": "default_deny"}
        if path != COMPANY_DECISION:
            # An uninstalled company package: OPA answers with no result;
            # the handler below turns this marker into that answer.
            return {"no_result": True}
        if (
            policy_input.get("lifecycle") == "standing"
            and policy_input.get("company") == COMPANY
            and policy_input.get("role") == "pm"
        ):
            if tool in (
                "issues.create",
                "issues.read",
                "issues.comment",
            ):
                return {"allow": True, "rule": f"role_pm_{tool}"}
            if tool == "crew.request" and args.get("crew_template") in (
                "feature-delivery",
                "feature-review",
            ):
                # The compiled cap and budget denies, reading
                # the enriched input exactly like the real bundle.
                if policy_input.get("active_spawns", 0) >= 2:
                    return {"allow": False, "rule": "crew_request_over_cap"}
                if policy_input.get("budget_headroom") is False:
                    return {"allow": False, "rule": "crew_request_no_budget"}
                return {"allow": True, "rule": "role_pm_crew_request"}
            return {"allow": False, "rule": "default_deny"}
        if (
            policy_input.get("lifecycle") == "ephemeral"
            and policy_input.get("company") == COMPANY
            and policy_input.get("crew_template") == "feature-review"
            and policy_input.get("crew_role") == "reviewer"
        ):
            if tool in ("git.read", "git.comment", "issues.comment"):
                return {"allow": True, "rule": f"crew_feature_review_reviewer_{tool}"}
            return {"allow": False, "rule": "default_deny"}
        if (
            policy_input.get("lifecycle") == "standing"
            and policy_input.get("company") == COMPANY
            and policy_input.get("role") == "backend-engineer"
        ):
            if tool.startswith("sandbox.") and policy_input.get("run_binding_verified"):
                return {"allow": True, "rule": f"role_backend_engineer_{tool}"}
            if tool == "git.open_pr":
                branch = args.get("branch", "")
                if branch.lower() in {"main", "master"}:
                    return {"allow": False, "rule": "git_open_pr_protected_branch"}
                if not FEATURE_BRANCH_RE.match(branch):
                    return {"allow": False, "rule": "git_open_pr_branch_pattern"}
                if "paths" not in args:
                    return {"allow": False, "rule": "git_open_pr_paths_missing"}
                if any(p.startswith(".github/workflows/") for p in args["paths"]):
                    return {"allow": False, "rule": "git_open_pr_denied_paths"}
                return {"allow": True, "rule": "role_backend_engineer_git_open_pr"}
        return {"allow": False, "rule": "default_deny"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "opa.test":
            if request.url.path in (COMPANY_DECISION, PLATFORM_DECISION):
                policy_input = json.loads(request.content)["input"]
                self.policy_inputs.append(policy_input)
                self.decision_paths.append(request.url.path)
                decision = self.decide(policy_input, request.url.path)
                if decision.get("no_result"):
                    return httpx.Response(200, json={})
                return httpx.Response(200, json={"result": decision})
            # Any other path: no document there (matches real OPA).
            return httpx.Response(200, json={})
        if request.url.host == "sbx.test":
            if self.sandbox_error is not None:
                return httpx.Response(self.sandbox_error, text="sandbox boom")
            path = request.url.path
            self.sandbox_calls.append((path, json.loads(request.content)))
            return httpx.Response(200, json=SANDBOX_RESPONSES[path])
        if request.url.host == "github.test":
            if self.github_error is not None:
                return httpx.Response(self.github_error, json={"message": "github boom"})
            payload = json.loads(request.content) if request.content else {}
            self.github_calls.append((request.url.path, payload, dict(request.headers)))
            return self._github(request, payload)
        return httpx.Response(502, text=f"unexpected host {request.url.host}")

    @staticmethod
    def _github(request: httpx.Request, payload: dict) -> httpx.Response:
        """Canned GitHub REST answers for the tool surface."""
        path, method = request.url.path, request.method

        def issue(number: int = 7, **overrides) -> dict:
            return {
                "number": number,
                "title": "an issue",
                "state": "open",
                "body": "details",
                "html_url": f"https://github.test/i/{number}",
                **overrides,
            }

        if method == "POST" and path.endswith("/pulls"):
            return httpx.Response(201, json={"html_url": PR_URL})
        if method == "POST" and path.endswith("/comments"):
            return httpx.Response(201, json={"html_url": "https://github.test/c/1"})
        if method == "POST" and path.endswith("/issues"):
            return httpx.Response(
                201,
                json=issue(title=payload.get("title", ""), body=payload.get("body", "")),
            )
        if method == "PATCH" and "/issues/" in path:
            return httpx.Response(200, json=issue(**payload))
        if method == "GET" and path.endswith("/issues"):
            # The listing includes a PR-shaped item the backlog must drop.
            return httpx.Response(
                200,
                json=[
                    issue(),
                    issue(number=8, pull_request={"url": "x"}),
                ],
            )
        if method == "GET" and "/issues/" in path:
            # Number 8 is the PR-shaped item (as in the listing above):
            # GitHub serves PRs from the issues endpoint too.
            if path.endswith("/8"):
                return httpx.Response(
                    200, json=issue(number=8, pull_request={"url": "x"})
                )
            return httpx.Response(200, json=issue())
        if method == "GET" and path.endswith("/files"):
            return httpx.Response(
                200,
                json=[
                    {
                        "filename": "app.py",
                        "additions": 3,
                        "deletions": 1,
                        "patch": "@@ -1 +1,3 @@",
                    },
                ],
            )
        if method == "GET" and "/pulls/" in path:
            return httpx.Response(
                200,
                json={
                    "number": 5,
                    "title": "a pr",
                    "state": "open",
                    "body": "",
                    "html_url": PR_URL,
                    "merged": False,
                    "head": {"ref": "feature/x"},
                    "base": {"ref": "main"},
                },
            )
        if method == "PUT" and path.endswith("/merge"):
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})
        return httpx.Response(404, json={"message": f"unstubbed {method} {path}"})


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def backends() -> Backends:
    return Backends()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    """The registry audit store, in memory — the interface is the same
    object the gateway writes through."""
    return InMemoryAuditStore()


@pytest.fixture
def settings() -> McpGatewaySettings:
    return McpGatewaySettings(
        opa_url=OPA_URL,
        git_token=GIT_TOKEN,
        base_ref="main",
        github_api_url=GITHUB_API_URL,
    )


@pytest.fixture
def instances() -> InMemoryAgentInstances:
    """The registry rows workload identity maps ServiceAccounts through —
    seeded with the standing agents the test tokens resolve to."""
    store = InMemoryAgentInstances()
    store.upsert(
        AGENT_ID,
        COMPANY,
        "backend-engineer",
        "backend-engineer",
        "frontier",
        namespace=COMPANY_NS,
    )
    store.upsert(
        "pm-1",
        COMPANY,
        "pm",
        "pm",
        "standard",
        namespace=COMPANY_NS,
    )
    return store


@pytest.fixture
def participants() -> InMemoryRunParticipants:
    return InMemoryRunParticipants()


@pytest.fixture
def crew_requests() -> InMemoryCrewRequests:
    return InMemoryCrewRequests()


@pytest.fixture
def ledger() -> InMemoryBudgetLedger:
    return InMemoryBudgetLedger()


@pytest.fixture
def companies() -> InMemoryCompanies:
    store = InMemoryCompanies()
    store.upsert(
        COMPANY,
        {
            "namespace": COMPANY_NS,
            "budgets": {"per_run_usd": 5.0},
        },
    )
    return store


@pytest.fixture
async def app(
    settings: McpGatewaySettings,
    backends: Backends,
    audit: InMemoryAuditStore,
    instances: InMemoryAgentInstances,
    participants: InMemoryRunParticipants,
    crew_requests: InMemoryCrewRequests,
    ledger: InMemoryBudgetLedger,
    companies: InMemoryCompanies,
):
    stub_client = httpx.AsyncClient(transport=httpx.MockTransport(backends.handler))
    application = create_app(
        settings,
        stub_client,
        sandboxes=InMemorySandboxRegistrations(),
        audit=audit,
        instances=instances,
        participants=participants,
        runs=InMemoryRuns(),
        crew_requests=crew_requests,
        ledger=ledger,
        companies=companies,
        token_reviewer=FakeReviewer(),
    )
    async with application.state.fastmcp.session_manager.run():
        yield application
    await stub_client.aclose()


def http_client(app, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://gateway.test",
        headers=headers,
    )


@asynccontextmanager
async def mcp_session(app, token: str):
    async with http_client(app, token) as client:
        async with streamable_http_client(
            "http://gateway.test/mcp", http_client=client
        ) as (read, write, _):
            async with ClientSession(read, write) as session:
                await session.initialize()
                yield session


async def register_sandbox(app, run_id: str, token: str = RUNNER_TOKEN) -> httpx.Response:
    """Register a run's sandbox exactly as the orchestrator does — namespace
    included, because the registration's namespace is what a standing
    caller's run binding is verified against."""
    async with http_client(app, token) as client:
        return await client.post(
            "/admin/sandboxes",
            json={"run_id": run_id, "url": SANDBOX_URL, "namespace": COMPANY_NS},
        )


def audit_events(audit: InMemoryAuditStore) -> list[dict]:
    return [event.model_dump() for event in audit.events]


# --- local git fixtures (TARGET_REPO stand-in, agent-side bundles) ------------


def git(*args: str, cwd: Path | None = None) -> str:
    proc = subprocess.run(
        ["git", "-c", "user.name=test", "-c", "user.email=test@poseidon.local", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=True,
    )
    return proc.stdout


@pytest.fixture
def target_repo(tmp_path: Path) -> Path:
    """Bare repo standing in for TARGET_REPO, seeded with one commit on main."""
    bare = tmp_path / "example" / "target.git"
    bare.parent.mkdir(parents=True)
    git("init", "--bare", "--initial-branch=main", str(bare))
    seed = tmp_path / "seed"
    git("init", "--initial-branch=main", str(seed))
    (seed / "README.md").write_text("# target repo\n")
    (seed / "app.py").write_text("def hello():\n    return 'hi'\n")
    git("add", "-A", cwd=seed)
    git("commit", "-m", "initial commit", cwd=seed)
    git("push", str(bare), "main", cwd=seed)
    return bare


def make_bundle(
    target_repo: Path, tmp_path: Path, branch: str, files: dict[str, str]
) -> str:
    """Do what the sandbox does: commit `files` on `branch`, bundle base..branch."""
    work = tmp_path / f"work-{branch.replace('/', '-')}"
    git("clone", str(target_repo), str(work))
    if branch != "main":
        git("checkout", "-b", branch, cwd=work)
    for path, content in files.items():
        target = work / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content)
    git("add", "-A", cwd=work)
    git("commit", "-m", f"changes on {branch}", cwd=work)
    bundle = work / "out.bundle"
    git("bundle", "create", str(bundle), f"origin/main..{branch}", cwd=work)
    return base64.b64encode(bundle.read_bytes()).decode()
