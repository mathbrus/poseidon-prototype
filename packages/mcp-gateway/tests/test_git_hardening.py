"""git_backend hardening: the transport set is closed, caller-supplied
repo/ref/branch strings can never be read as git options, alternate
transports, or refspec rewrites, and the platform token never leaves in
an error."""

import json
import subprocess
import traceback

import httpx
import pytest
from conftest import GIT_TOKEN, PM_TOKEN, REPO, RUN_ID, audit_events, mcp_session

from common.topology import PLATFORM_NAMESPACE
from mcp_gateway import git_backend
from mcp_gateway.config import McpGatewaySettings
from mcp_gateway.git_backend import (
    GIT_TIMEOUT_S,
    GitBackend,
    GitBackendError,
    _require_safe_argument,
    _require_safe_overlay_path,
    _require_safe_ref,
)

TOKEN = "tok-123"
CREDENTIALED = f"https://x-access-token:{TOKEN}@github.com/org/repo"


def backend(http_client: httpx.AsyncClient | None = None) -> GitBackend:
    return GitBackend(
        McpGatewaySettings(
            platform_namespace=PLATFORM_NAMESPACE,
            opa_url="http://opa.test:8181",
            git_token=TOKEN,
        ),
        http_client=http_client,
    )


# --- the clone-URL transport allowlist --------------------------------------


def test_local_paths_pass_through():
    assert backend()._clone_url("/tmp/repos/target.git") == "/tmp/repos/target.git"
    assert backend()._clone_url("file:///tmp/x") == "file:///tmp/x"


def test_host_paths_rewrite_to_credentialed_https():
    url = backend()._clone_url("github.com/org/repo")
    assert url == "https://x-access-token:tok-123@github.com/org/repo"
    assert (
        backend()
        ._clone_url("https://github.com/org/repo")
        .startswith("https://x-access-token:")
    )


@pytest.mark.parametrize(
    "repo",
    [
        "ext::sh -c whoami",  # git's command-executing transport
        "fd::17",
        "ssh://git@github.com/org/repo",
        "http://github.com/org/repo",
        "--upload-pack=evil",
        "-oProxyCommand=evil",
        "github.com/org/repo name",  # whitespace smuggling
        "",
    ],
)
def test_dangerous_repo_values_are_refused(repo):
    with pytest.raises(GitBackendError):
        backend()._clone_url(repo)


# --- ref / branch validation ------------------------------------------------


@pytest.mark.parametrize("ref", ["main", "feature/add-x", "v1.2.3", "abc123"])
def test_ordinary_refs_pass(ref):
    assert _require_safe_ref("ref", ref) == ref


@pytest.mark.parametrize(
    "ref",
    [
        "--help",
        "-b",
        "main:refs/heads/other",  # refspec rewrite
        "a..b",  # range
        "/leading",
        "trailing/",
        "has space",
        "has\ttab",
        "",
    ],
)
def test_dangerous_refs_are_refused(ref):
    with pytest.raises(GitBackendError):
        _require_safe_ref("ref", ref)


def test_safe_argument_rejects_option_shaped_values():
    with pytest.raises(GitBackendError):
        _require_safe_argument("repo", "--mirror")
    assert _require_safe_argument("repo", "github.com/o/r") == "github.com/o/r"


# --- overlay paths (the composed-archive tree) -------------------------------


@pytest.mark.parametrize("path", ["tests/contract/", "tests/contract", "specs/api.py"])
def test_ordinary_overlay_paths_pass(path):
    assert _require_safe_overlay_path(path) == path


@pytest.mark.parametrize(
    "path",
    [
        "--help",  # option-shaped
        "/etc/passwd",  # absolute
        "../outside",  # escapes the clone
        "tests/../../outside",  # escapes via the middle
        "has space",
        "",
    ],
)
def test_dangerous_overlay_paths_are_refused(path):
    """The backend removes and re-checks-out at overlay paths, so a path
    that is an option, absolute, or `..`-escaping must never reach git or
    the filesystem."""
    with pytest.raises(GitBackendError):
        _require_safe_overlay_path(path)


# --- the token never leaves in an error --------------------------------------


def told(error: BaseException) -> str:
    """Everything a raised error says, the errors it chains included."""
    return "".join(traceback.format_exception(error))


def test_a_failed_git_command_names_its_url_without_the_token(monkeypatch):
    """git names the URL it failed on, credentials and all: the backend's
    error keeps git's words with the token blanked."""

    def run(cmd, **kwargs):
        stderr = f"fatal: unable to access '{CREDENTIALED}/': returned error: 403"
        return subprocess.CompletedProcess(cmd, 128, "", stderr)

    monkeypatch.setattr(git_backend.subprocess, "run", run)
    with pytest.raises(GitBackendError) as caught:
        backend()._git(None, "clone", "--", CREDENTIALED, "clone")
    assert str(caught.value) == (
        "git clone failed: fatal: unable to access "
        "'https://x-access-token:***@github.com/org/repo/': returned error: 403"
    )
    assert TOKEN not in told(caught.value)


def test_a_timed_out_git_command_chains_nothing_that_names_the_token(monkeypatch):
    """A timeout's own message is the whole command line, the credentialed
    clone URL in it: the backend's error says what timed out, and carries
    no cause that says more."""

    def run(cmd, **kwargs):
        raise subprocess.TimeoutExpired(cmd, kwargs["timeout"], stderr=CREDENTIALED)

    monkeypatch.setattr(git_backend.subprocess, "run", run)
    with pytest.raises(GitBackendError) as caught:
        backend()._git(None, "clone", "--", CREDENTIALED, "clone")
    assert str(caught.value) == f"git clone timed out after {GIT_TIMEOUT_S}s"
    assert TOKEN not in told(caught.value)


@pytest.mark.anyio
async def test_a_git_host_error_keeps_its_body_without_the_token():
    """A host that echoes the request's credential in its error body: the
    backend's error carries the status and the body, the token blanked."""

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(401, text=f"bad {request.headers['authorization']}")

    host = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    with pytest.raises(GitBackendError) as caught:
        await backend(host).read_issues("github.com/org/repo", 7)
    assert str(caught.value) == (
        "GitHub GET /repos/org/repo/issues/7 failed: 401 bad Bearer ***"
    )
    assert TOKEN not in told(caught.value)


@pytest.mark.anyio
async def test_an_agent_reads_a_git_host_error_without_the_token(
    app, backends, audit, monkeypatch
):
    """Through the pipeline: the error an agent reads back from a failed
    git host call, and the call's audit row, carry no token."""
    monkeypatch.setattr(
        backends,
        "_github",
        lambda request, payload: httpx.Response(
            401, text=f"bad {request.headers['authorization']}"
        ),
    )
    async with mcp_session(app, PM_TOKEN) as session:
        result = await session.call_tool(
            "issues.read", {"run_id": RUN_ID, "repo": REPO, "number": 7}
        )
    assert result.isError
    assert "failed: 401 bad Bearer ***" in result.content[0].text
    assert GIT_TOKEN not in result.content[0].text
    (event,) = audit_events(audit)
    assert event["decision"] == "allow" and event["backend_status"] == "error"
    assert GIT_TOKEN not in json.dumps(audit_events(audit))
