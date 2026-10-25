"""Gateway unit-test fixtures: stubbed OPA, stubbed sandbox and stubbed
GitHub API behind one httpx.MockTransport, the real MCP app served over an
in-process ASGI transport, and a local bare git repo standing in for
TARGET_REPO (real git subprocesses, no network).

The callers are the ones a cluster has: the platform identity, the
console, the standing conductor `pm-1`, and the run's ephemeral agents,
bound to `RUN_ID` under the conductor the way the orchestrator binds them.
Their tokens resolve through a fake TokenReview (workload identity)."""

import base64
import json
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

import httpx
import pytest
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

from blueprint.tools import (
    ARCHITECTURE_REQUEST,
    GIT_CLOSE_PR,
    ISSUES_CLOSE,
    ISSUES_DISMISS,
    REPO_PURGE,
    RUN_SCOPED,
    SANDBOX_TOOLS,
)
from common.authn import Principal, ServiceAccount
from common.policy import DEFAULT_DENY, PLATFORM_DECISION_PATH, company_decision_path
from common.topology import (
    COMPANY_NAMESPACE,
    CONSOLE_SERVICE_ACCOUNT,
    ORCHESTRATOR_SERVICE_ACCOUNT,
    PLATFORM_NAMESPACE,
)
from mcp_gateway.app import create_app
from mcp_gateway.config import McpGatewaySettings
from mcp_gateway.git_backend import GITHUB_API_URL
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryCompanies,
    InMemoryFiledTickets,
    InMemoryRunParticipants,
    InMemoryRuns,
    InMemorySandboxRegistrations,
)

AGENT_TOKEN = "agent-secret"
WORKER_TOKEN = "worker-secret"
PM_TOKEN = "pm-secret"
REVIEWER_TOKEN = "reviewer-secret"
CONSOLE_TOKEN = "console-secret"
GIT_TOKEN = "git-platform-secret"
OPA_URL = "http://opa.test"
SANDBOX_URL = "http://sbx.test:8000"
REPO = "github.test/example/target"
PR_URL = "https://github.test/example/pull/1"
# The open PR's head commit, what a pinned merge must name.
HEAD_SHA = "f00d5ha"
BRANCH_PATTERN = r"^feature/[a-z0-9-]+$"
FEATURE_BRANCH_RE = re.compile(BRANCH_PATTERN)

COMPANY = "hello-saas"
RUN_ID = "run-01TESTULID"
# The standing agent every run's ephemeral agents are spawned under.
CONDUCTOR = "pm-1"
DELIVERY_CREW = "feature-delivery"
REVIEW_CREW = "feature-review"
COMPANY_DECISION = f"/v1/data/{company_decision_path(COMPANY)}"
PLATFORM_DECISION = f"/v1/data/{PLATFORM_DECISION_PATH}"


def ephemeral_id(crew: str, role: str, run_id: str = RUN_ID) -> str:
    """An ephemeral agent's id, as the orchestrator derives it."""
    return f"{crew}-{role}-{run_id}"


def ephemeral_service_account(agent_id: str) -> str:
    """The per-run ServiceAccount the orchestrator creates for an ephemeral
    agent: the id lowercased, as a Kubernetes name must be."""
    return f"ephemeral-{agent_id}".lower()[:63].rstrip("-")


# The run's ephemeral implementer, the caller of the sandbox tools and of
# `git.open_pr`, and its ephemeral reviewer.
AGENT_ID = ephemeral_id(DELIVERY_CREW, "implementer")
REVIEWER_ID = ephemeral_id(REVIEW_CREW, "reviewer")


def bind_ephemeral(
    participants: InMemoryRunParticipants,
    crew: str,
    role: str,
    run_id: str = RUN_ID,
    parent: str = CONDUCTOR,
) -> str:
    """Bind an ephemeral agent to its run the way the orchestrator does: one
    `run_participants` row, under its parent, in the company namespace,
    keyed by the ServiceAccount its token names. Returns the agent id."""
    agent_id = ephemeral_id(crew, role, run_id)
    participants.add(
        agent_id,
        run_id,
        parent,
        crew,
        role,
        company=COMPANY,
        namespace=COMPANY_NAMESPACE,
        service_account=ephemeral_service_account(agent_id),
    )
    return agent_id


class FakeReviewer:
    """TokenReview double: the test tokens resolve to the same
    ServiceAccounts the kind cluster would report. A test adds a caller of
    its own to `accounts`."""

    def __init__(self) -> None:
        self.accounts = {
            AGENT_TOKEN: ServiceAccount(
                COMPANY_NAMESPACE, ephemeral_service_account(AGENT_ID)
            ),
            WORKER_TOKEN: ServiceAccount(
                PLATFORM_NAMESPACE, ORCHESTRATOR_SERVICE_ACCOUNT
            ),
            PM_TOKEN: ServiceAccount(COMPANY_NAMESPACE, CONDUCTOR),
            REVIEWER_TOKEN: ServiceAccount(
                COMPANY_NAMESPACE, ephemeral_service_account(REVIEWER_ID)
            ),
            CONSOLE_TOKEN: ServiceAccount(PLATFORM_NAMESPACE, CONSOLE_SERVICE_ACCOUNT),
        }

    async def review(self, token: str, audience: str) -> Principal | None:
        account = self.accounts.get(token)
        return None if account is None else Principal(account.username, account)


SANDBOX_RESPONSES = {
    "/files/read": {"content": "hello from sandbox"},
    "/files/write": {"ok": True},
    "/files/edit": {"ok": True, "snippet": "     1\tx"},
    "/files/list": {"files": ["README.md"], "omitted": 0},
    "/exec": {"exit_code": 0, "stdout": "ran", "stderr": ""},
    "/tests": {"exit_code": 0, "stdout": "1 passed", "stderr": ""},
    "/bundle": {"bundle_b64": "YnVuZGxl", "base_ref": "main"},
}

# What the OPA stub lets each test caller call, by lifecycle and role.
ALLOWED = {
    ("platform", "platform"): {
        "/admin/sandboxes",
        "/admin/sandboxes.delete",
        "repo.archive",
        "repo.read_file",
        "repo.present",
        "sandbox.run_tests",
        "git.merge_pr",
        GIT_CLOSE_PR,
        ISSUES_CLOSE,
    },
    ("console", "console"): {REPO_PURGE},
    ("standing", "pm"): {
        "issues.create",
        "issues.read",
        "issues.comment",
        ISSUES_DISMISS,
    },
    ("ephemeral", "implementer"): {*SANDBOX_TOOLS, ARCHITECTURE_REQUEST, "git.open_pr"},
    ("ephemeral", "reviewer"): {"git.read", "git.comment"},
}


class Backends:
    """Scriptable stand-ins for OPA, the sandbox tool server and GitHub.

    The OPA stub is not the compiled policy: it lets each test caller call
    the few tools the tests drive (`ALLOWED`) and refuses the rest, so the
    tests watch the gateway's own work — which decision path it asks, which
    facts it verifies and injects into the input, how it audits and
    explains a refusal. It reads those facts the way a compiled bundle
    does: a run-scoped tool needs `run_binding_verified`; `git.open_pr`
    reads the changed `paths`; `architecture.request` and `issues.dismiss`
    read the counts and facts the gateway injects. Which role may call
    what is proven against real bundles, in the compiler's tests.
    `force_allow` short-circuits it for tests that need an allow despite
    no registration.
    """

    def __init__(self) -> None:
        self.sandbox_calls: list[tuple[str, dict]] = []
        self.policy_inputs: list[dict] = []
        self.decision_paths: list[str] = []
        self.github_calls: list[tuple[str, dict, dict]] = []
        self.force_allow = False
        self.sandbox_error: int | None = None

    def decide(self, policy_input: dict, path: str) -> dict:
        if self.force_allow:
            return {"allow": True, "rule": "forced_allow"}
        tool, args = policy_input["tool"], policy_input["args"]
        lifecycle, role = policy_input["lifecycle"], policy_input["role"]
        platform = lifecycle == "platform"
        if (path == PLATFORM_DECISION) != platform:
            return deny(DEFAULT_DENY)
        if tool not in ALLOWED.get((lifecycle, role), ()):
            return deny(DEFAULT_DENY)
        if tool in RUN_SCOPED and not policy_input["run_binding_verified"]:
            return deny(DEFAULT_DENY)
        refusal = self._refusal(tool, args, policy_input)
        if refusal:
            return deny(refusal)
        caller = lifecycle if lifecycle == role else f"{lifecycle}_{role}"
        return {"allow": True, "rule": f"{caller}_{tool}"}

    @staticmethod
    def _refusal(tool: str, args: dict, policy_input: dict) -> str | None:
        """The rule that refuses an otherwise allowed call, read off its
        arguments and the facts the gateway injected."""
        if tool == "git.open_pr":
            branch = args.get("branch", "")
            if branch.lower() in {"main", "master"}:
                return "git_open_pr_protected_branch"
            if not FEATURE_BRANCH_RE.match(branch):
                return "git_open_pr_branch_pattern"
            if "paths" not in args:
                return "git_open_pr_paths_missing"
            if any(p.startswith(".github/workflows/") for p in args["paths"]):
                return "git_open_pr_denied_paths"
        if tool == ARCHITECTURE_REQUEST and policy_input["architecture_requests"] >= 1:
            return "architecture_request_over_cap"
        if tool == ISSUES_DISMISS:
            target = policy_input.get("dismiss_target")
            if target is None:
                return "issues_dismiss_facts_missing"
            if policy_input["workflow"] != "scope" or not policy_input["run_live"]:
                return "issues_dismiss_not_scoping"
            if target["bootstrap"]:
                return "issues_dismiss_bootstrap_ticket"
            if target["in_flight"]:
                return "issues_dismiss_in_flight"
            if policy_input["dismissals"] >= 3:
                return "issues_dismiss_over_cap"
        if tool == REPO_PURGE and (args["repo"], args["base_ref"]) != (REPO, "main"):
            return DEFAULT_DENY
        return None

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "opa.test":
            if request.url.path in (COMPANY_DECISION, PLATFORM_DECISION):
                policy_input = json.loads(request.content)["input"]
                self.policy_inputs.append(policy_input)
                self.decision_paths.append(request.url.path)
                decision = self.decide(policy_input, request.url.path)
                return httpx.Response(200, json={"result": decision})
            # Any other path: no document there (matches real OPA).
            return httpx.Response(200, json={})
        if request.url.host == "sbx.test":
            if self.sandbox_error is not None:
                return httpx.Response(self.sandbox_error, text="sandbox boom")
            path = request.url.path
            self.sandbox_calls.append((path, json.loads(request.content)))
            return httpx.Response(200, json=SANDBOX_RESPONSES[path])
        if request.url.host == urlsplit(GITHUB_API_URL).hostname:
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
            # Number 9 is an issue somebody already closed; number 10 is the
            # ticket the platform filed for the absent strategy.
            if path.endswith("/9"):
                return httpx.Response(200, json=issue(number=9, state="closed"))
            if path.endswith("/10"):
                return httpx.Response(
                    200, json=issue(number=10, title="Bootstrap: strategy")
                )
            return httpx.Response(200, json=issue())
        if method == "GET" and "/contents/" in path:
            # The repository at base_ref: the strategy and the contract
            # tree exist, anything else does not.
            if path.endswith("/contents/tests/contract"):
                return httpx.Response(
                    200, json=[{"type": "file", "path": "tests/contract/test_api.py"}]
                )
            if path.endswith("/contents/docs/STRATEGY.md"):
                content = base64.b64encode(b"# Strategy\n\nShip slugs.\n").decode()
                return httpx.Response(
                    200, json={"type": "file", "encoding": "base64", "content": content}
                )
            return httpx.Response(404, json={"message": "Not Found"})
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
        if method == "GET" and "/git/ref/heads/" in path:
            # The base branch's tip, which a PR's own base.sha lags.
            return httpx.Response(200, json={"object": {"sha": "ba5e5ha"}})
        if method == "GET" and "/pulls/" in path:
            # Number 6 is a pull request somebody already closed.
            number = 6 if path.endswith("/6") else 5
            return httpx.Response(
                200,
                json={
                    "number": number,
                    "title": "a pr",
                    "state": "closed" if number == 6 else "open",
                    "body": "",
                    "html_url": PR_URL,
                    "merged": False,
                    "head": {
                        "ref": "feature/x",
                        "sha": HEAD_SHA,
                        "repo": {"full_name": "example/target"},
                    },
                    "base": {"ref": "main", "repo": {"full_name": "example/target"}},
                },
            )
        if method == "PATCH" and "/pulls/" in path:
            return httpx.Response(
                200,
                json={
                    "number": 5,
                    "head": {"ref": "feature/x", "repo": {"full_name": "example/target"}},
                    "base": {"ref": "main"},
                    **payload,
                },
            )
        if method == "DELETE" and "/git/refs/heads/" in path:
            return httpx.Response(204)
        if method == "PUT" and path.endswith("/merge"):
            # GitHub refuses a merge whose expected head is not the PR's.
            if payload.get("sha") != HEAD_SHA:
                return httpx.Response(409, json={"message": "Head branch was modified"})
            return httpx.Response(200, json={"merged": True, "sha": "abc123"})
        return httpx.Response(404, json={"message": f"unstubbed {method} {path}"})


def deny(rule: str) -> dict:
    return {"allow": False, "rule": rule}


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
        platform_namespace=PLATFORM_NAMESPACE,
        opa_url=OPA_URL,
        git_token=GIT_TOKEN,
    )


@pytest.fixture
def instances() -> InMemoryAgentInstances:
    """The `agent_instances` rows standing ServiceAccounts map through: the
    conductor."""
    store = InMemoryAgentInstances()
    store.upsert(CONDUCTOR, COMPANY, CONDUCTOR, "pm", namespace=COMPANY_NAMESPACE)
    return store


@pytest.fixture
def participants() -> InMemoryRunParticipants:
    """The `run_participants` rows ephemeral ServiceAccounts map through:
    the run's implementer, bound to `RUN_ID` under the conductor."""
    store = InMemoryRunParticipants()
    bind_ephemeral(store, DELIVERY_CREW, "implementer")
    return store


@pytest.fixture
def filed() -> InMemoryFiledTickets:
    return InMemoryFiledTickets()


@pytest.fixture
def runs() -> InMemoryRuns:
    return InMemoryRuns()


@pytest.fixture
def companies() -> InMemoryCompanies:
    store = InMemoryCompanies()
    store.upsert(
        COMPANY,
        {
            "namespace": COMPANY_NAMESPACE,
            "repo": {"url": REPO, "base_ref": "main"},
            "documents": {
                "strategy": "docs/STRATEGY.md",
                "architecture": "docs/ARCHITECTURE.md",
            },
            # The policies the OPA stub enforces, as the applied spec
            # states them — what a denial is explained from.
            "policies": {
                "branch_protection": {
                    "protected": ["main"],
                    "allowed_pattern": BRANCH_PATTERN,
                },
                "deny_paths": [".github/workflows/"],
            },
            "budgets": {"per_run_usd": 5.0},
        },
    )
    return store


@pytest.fixture
def reviewer() -> FakeReviewer:
    return FakeReviewer()


@pytest.fixture
async def app(
    settings: McpGatewaySettings,
    backends: Backends,
    audit: InMemoryAuditStore,
    instances: InMemoryAgentInstances,
    participants: InMemoryRunParticipants,
    companies: InMemoryCompanies,
    filed: InMemoryFiledTickets,
    runs: InMemoryRuns,
    reviewer: FakeReviewer,
):
    stub_client = httpx.AsyncClient(transport=httpx.MockTransport(backends.handler))
    application = create_app(
        settings,
        stub_client,
        sandboxes=InMemorySandboxRegistrations(),
        audit=audit,
        instances=instances,
        participants=participants,
        runs=runs,
        companies=companies,
        filed=filed,
        token_reviewer=reviewer,
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


async def register_sandbox(
    app, run_id: str, token: str = WORKER_TOKEN, namespace: str = COMPANY_NAMESPACE
) -> httpx.Response:
    """Register a run's sandbox exactly as the orchestrator does — namespace
    included, because the registration's namespace is what a standing
    caller's run binding is verified against."""
    async with http_client(app, token) as client:
        return await client.post(
            "/admin/sandboxes",
            json={"run_id": run_id, "url": SANDBOX_URL, "namespace": namespace},
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
