"""git_backend argument hardening: the transport set is closed and
caller-supplied repo/ref/branch strings can never be read as git options,
alternate transports, or refspec rewrites."""

import pytest

from common.topology import PLATFORM_NAMESPACE
from mcp_gateway.config import McpGatewaySettings
from mcp_gateway.git_backend import (
    GitBackend,
    GitBackendError,
    _require_safe_argument,
    _require_safe_overlay_path,
    _require_safe_ref,
)


def backend() -> GitBackend:
    return GitBackend(
        McpGatewaySettings(
            platform_namespace=PLATFORM_NAMESPACE,
            opa_url="http://opa.test:8181",
            git_token="tok-123",
        ),
        http_client=None,
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
