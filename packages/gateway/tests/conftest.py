"""Gateway unit-test fixtures: stubbed OPA, stubbed sandbox and stubbed
GitHub API behind one httpx.MockTransport, the real MCP app served over an
in-process ASGI transport, and a local bare git repo standing in for
TARGET_REPO (real git subprocesses, no network)."""

import base64
import json
import re
import subprocess
from contextlib import asynccontextmanager
from pathlib import Path

import httpx
import pytest

from gateway.app import create_app
from gateway.config import GatewaySettings
from registry.memory import InMemoryAuditStore, InMemorySandboxRegistrations
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client

AGENT_TOKEN = "agent-secret"
RUNNER_TOKEN = "runner-secret"
GIT_TOKEN = "git-platform-secret"
OPA_URL = "http://opa.test"
SANDBOX_URL = "http://sbx.test:8000"
GITHUB_API_URL = "http://github.test"
PR_URL = "https://github.test/example/pull/1"
FEATURE_BRANCH_RE = re.compile(r"^feature/[a-z0-9-]+$")

SANDBOX_RESPONSES = {
    "/files/read": {"content": "hello from sandbox"},
    "/files/write": {"ok": True},
    "/files/list": {"entries": [{"name": "README.md", "type": "file"}]},
    "/exec": {"exit_code": 0, "stdout": "ran", "stderr": ""},
    "/tests": {"exit_code": 0, "stdout": "1 passed", "stderr": ""},
    "/bundle": {"bundle_b64": "YnVuZGxl", "base_ref": "main"},
}


class Backends:
    """Scriptable stand-ins for OPA, the sandbox tool server and GitHub.

    The OPA stub mirrors the real policy shape (policies/poc.rego):
    sandbox.* allowed for backend-engineer on assigned runs, git.open_pr
    only onto feature branches and never touching workflows, repo.archive
    and /admin/sandboxes for the runner, default deny otherwise.
    `force_allow` short-circuits it for tests that need an allow despite
    no registration.
    """

    def __init__(self) -> None:
        self.assignments: dict[str, list[str]] = {}
        self.sandbox_calls: list[tuple[str, dict]] = []
        self.policy_inputs: list[dict] = []
        self.github_calls: list[tuple[str, dict, dict]] = []
        self.force_allow = False
        self.sandbox_error: int | None = None
        self.github_error: int | None = None

    def decide(self, policy_input: dict) -> dict:
        if self.force_allow:
            return {"allow": True, "rule": "forced_allow"}
        tool, role = policy_input["tool"], policy_input["role"]
        args = policy_input.get("args", {})
        if tool.startswith("sandbox.") and role == "backend-engineer":
            if policy_input["run_id"] in self.assignments.get(policy_input["agent_id"], []):
                return {"allow": True, "rule": "sandbox_assigned_run"}
        if tool == "git.open_pr":
            branch = args.get("branch", "")
            if branch.lower() in {"main", "master"}:
                return {"allow": False, "rule": "git_open_pr_protected_branch"}
            if not FEATURE_BRANCH_RE.match(branch):
                return {"allow": False, "rule": "git_open_pr_branch_pattern"}
            if any(p.startswith(".github/workflows/") for p in args.get("paths", [])):
                return {"allow": False, "rule": "git_open_pr_workflow_paths"}
            return {"allow": True, "rule": "git_open_pr_feature_branch"}
        if tool == "repo.archive" and role == "runner":
            return {"allow": True, "rule": "repo_archive_runner"}
        if tool == "/admin/sandboxes" and role == "runner":
            return {"allow": True, "rule": "admin_sandboxes_runner"}
        return {"allow": False, "rule": "default_deny"}

    def handler(self, request: httpx.Request) -> httpx.Response:
        if request.url.host == "opa.test":
            if request.url.path == "/v1/data/poseidon/poc/decision":
                policy_input = json.loads(request.content)["input"]
                self.policy_inputs.append(policy_input)
                return httpx.Response(200, json={"result": self.decide(policy_input)})
            if request.url.path == "/v1/data/runs/assignments":
                self.assignments = json.loads(request.content)
                return httpx.Response(204)
            return httpx.Response(404)
        if request.url.host == "sbx.test":
            if self.sandbox_error is not None:
                return httpx.Response(self.sandbox_error, text="sandbox boom")
            path = request.url.path
            self.sandbox_calls.append((path, json.loads(request.content)))
            return httpx.Response(200, json=SANDBOX_RESPONSES[path])
        if request.url.host == "github.test":
            if self.github_error is not None:
                return httpx.Response(self.github_error, json={"message": "github boom"})
            self.github_calls.append(
                (request.url.path, json.loads(request.content), dict(request.headers))
            )
            return httpx.Response(201, json={"html_url": PR_URL})
        return httpx.Response(502, text=f"unexpected host {request.url.host}")


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def backends() -> Backends:
    return Backends()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    """The registry audit store, in memory (plan step 8 moved the sink off
    JSONL; the interface is the same object the gateway writes through)."""
    return InMemoryAuditStore()


@pytest.fixture
def settings() -> GatewaySettings:
    return GatewaySettings(
        agent_bearer_token=AGENT_TOKEN,
        runner_bearer_token=RUNNER_TOKEN,
        opa_url=OPA_URL,
        git_token=GIT_TOKEN,
        base_ref="main",
        github_api_url=GITHUB_API_URL,
    )


@pytest.fixture
async def app(settings: GatewaySettings, backends: Backends, audit: InMemoryAuditStore):
    stub_client = httpx.AsyncClient(transport=httpx.MockTransport(backends.handler))
    application = create_app(
        settings,
        stub_client,
        sandboxes=InMemorySandboxRegistrations(),
        audit=audit,
    )
    async with application.state.fastmcp.session_manager.run():
        yield application
    await stub_client.aclose()


def http_client(app, token: str | None) -> httpx.AsyncClient:
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://gateway.test", headers=headers
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
    async with http_client(app, token) as client:
        return await client.post("/admin/sandboxes", json={"run_id": run_id, "url": SANDBOX_URL})


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


def make_bundle(target_repo: Path, tmp_path: Path, branch: str, files: dict[str, str]) -> str:
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
