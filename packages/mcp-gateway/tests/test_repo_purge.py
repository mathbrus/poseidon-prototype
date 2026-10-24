"""`repo.purge`: the repository reset a person asks for when deleting the
company with `--purge-remote`. The backend against a fake GitHub that
keeps state (pages, 204s, a protected branch), and the tool through the
gateway pipeline: the console's alone."""

import json
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
import pytest
from conftest import (
    CONSOLE_TOKEN,
    GIT_TOKEN,
    PM_TOKEN,
    REPO,
    audit_events,
    mcp_session,
)

from common.topology import PLATFORM_NAMESPACE
from mcp_gateway.config import McpGatewaySettings
from mcp_gateway.git_backend import EMPTY_TREE, GitBackend, GitBackendError

pytestmark = pytest.mark.anyio

SLUG = "/repos/example/target"


class FakeGitHub:
    """A repository on a git host: open pull requests and issues, branches,
    the refs a reset forces. Listings come in pages; a ref deletion answers
    204; a protected branch refuses a forced update."""

    def __init__(self, pulls: int, issues: int, branches: int, default: str) -> None:
        self.default = default
        self.pulls = {n: "open" for n in range(1, pulls + 1)}
        self.issues = {n: "open" for n in range(1000, 1000 + issues)}
        self.reasons: dict[int, str] = {}
        self.branches = {"main", default} | {f"feature/f{n}" for n in range(branches)}
        self.refs: dict[str, str] = {}
        self.protected: set[str] = set()
        self.commits: list[dict] = []

    def handler(self, request: httpx.Request) -> httpx.Response:
        path, method = unquote(request.url.path), request.method
        assert path.startswith(SLUG), path
        tail = path.removeprefix(SLUG)
        query = parse_qs(urlsplit(str(request.url)).query)
        body = json.loads(request.content) if request.content else {}
        if method == "GET" and tail == "":
            return httpx.Response(200, json={"default_branch": self.default})
        if method == "POST" and tail == "/git/commits":
            self.commits.append(body)
            return httpx.Response(201, json={"sha": f"root{len(self.commits)}"})
        if method == "PATCH" and tail.startswith("/git/refs/heads/"):
            branch = tail.removeprefix("/git/refs/heads/")
            if branch in self.protected:
                return httpx.Response(
                    422, json={"message": "Cannot force-push to this branch"}
                )
            self.refs[branch] = body["sha"]
            return httpx.Response(200, json={"object": {"sha": body["sha"]}})
        if method == "DELETE" and tail.startswith("/git/refs/heads/"):
            self.branches.discard(tail.removeprefix("/git/refs/heads/"))
            return httpx.Response(204)
        if method == "GET" and tail in ("/pulls", "/issues", "/branches"):
            assert query.get("state", ["open"]) == ["open"]
            return httpx.Response(200, json=self._page(tail, query))
        if method == "PATCH" and tail.startswith("/pulls/"):
            self.pulls[int(tail.rsplit("/", 1)[1])] = body["state"]
            return httpx.Response(200, json={})
        if method == "PATCH" and tail.startswith("/issues/"):
            number = int(tail.rsplit("/", 1)[1])
            self.issues[number] = body["state"]
            self.reasons[number] = body["state_reason"]
            return httpx.Response(200, json={})
        return httpx.Response(404, json={"message": f"unstubbed {method} {path}"})

    def _page(self, tail: str, query: dict) -> list[dict]:
        if tail == "/pulls":
            items = [{"number": n} for n, s in sorted(self.pulls.items()) if s == "open"]
        elif tail == "/issues":
            # GitHub lists open pull requests among the issues.
            items = [
                {"number": n, "pull_request": {"url": "x"}}
                for n, s in sorted(self.pulls.items())
                if s == "open"
            ] + [{"number": n} for n, s in sorted(self.issues.items()) if s == "open"]
        else:
            items = [{"name": name} for name in sorted(self.branches)]
        size, page = int(query["per_page"][0]), int(query["page"][0])
        return items[(page - 1) * size : page * size]


def backend(github: FakeGitHub) -> GitBackend:
    return GitBackend(
        McpGatewaySettings(
            platform_namespace=PLATFORM_NAMESPACE,
            opa_url="http://opa.test:8181",
            git_token=GIT_TOKEN,
        ),
        httpx.AsyncClient(transport=httpx.MockTransport(github.handler)),
    )


async def test_a_purge_resets_the_repository_across_every_page():
    """More of everything than one page holds: every open pull request and
    issue closed, every branch deleted but the base and default ones, and
    both forced onto one root commit on the empty tree."""
    github = FakeGitHub(pulls=130, issues=210, branches=105, default="trunk")
    result = await backend(github).purge(REPO, "main")

    assert result["reset"] == ["main", "trunk"]
    assert github.commits == [
        {"message": github.commits[0]["message"], "tree": EMPTY_TREE, "parents": []}
    ]
    assert github.refs == {"main": "root1", "trunk": "root1"}
    assert set(github.pulls.values()) == {"closed"}
    assert set(github.issues.values()) == {"closed"}
    assert set(github.reasons.values()) == {"not_planned"}
    assert github.branches == {"main", "trunk"}
    assert len(result["pull_requests_closed"]) == 130
    assert len(result["issues_closed"]) == 210  # pull requests are not issues
    assert len(result["branches_deleted"]) == 105


async def test_a_protected_base_refuses_the_purge_before_anything_changes():
    """The host refuses a forced update of a protected branch: the purge
    fails, naming why, with every pull request, issue and branch as it
    was."""
    github = FakeGitHub(pulls=2, issues=2, branches=2, default="main")
    github.protected = {"main"}
    with pytest.raises(GitBackendError, match="Cannot force-push"):
        await backend(github).purge(REPO, "main")
    assert set(github.pulls.values()) == {"open"}
    assert set(github.issues.values()) == {"open"}
    assert len(github.branches) == 3


async def test_a_purge_repeated_finds_nothing_left_to_close():
    github = FakeGitHub(pulls=1, issues=1, branches=1, default="main")
    await backend(github).purge(REPO, "main")
    again = await backend(github).purge(REPO, "main")
    assert again["pull_requests_closed"] == again["issues_closed"] == []
    assert again["branches_deleted"] == []


async def test_only_the_console_may_purge(app, backends, audit):
    """Through the pipeline: the console's call is allowed on the declared
    repository and audited under its own lifecycle; a standing agent's is
    refused before the git host hears of it."""
    async with mcp_session(app, PM_TOKEN) as session:
        refused = await session.call_tool(
            "repo.purge", {"run_id": "", "repo": REPO, "base_ref": "main"}
        )
    assert refused.isError
    assert backends.github_calls == []
    async with mcp_session(app, CONSOLE_TOKEN) as session:
        purged = await session.call_tool(
            "repo.purge", {"run_id": "", "repo": REPO, "base_ref": "main"}
        )
    assert not purged.isError, purged.content
    assert json.loads(purged.content[0].text)["reset"] == ["main"]
    rows = [(e["lifecycle"], e["decision"], e["rule"]) for e in audit_events(audit)]
    assert rows == [
        ("standing", "deny", "default_deny"),
        ("console", "allow", "console_repo_purge"),
    ]
