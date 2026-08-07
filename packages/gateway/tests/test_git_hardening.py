"""git_backend argument hardening: the transport set is closed and
caller-supplied repo/ref/branch strings can never be read as git options,
alternate transports, or refspec rewrites."""

import pytest

from gateway.config import GatewaySettings
from gateway.git_backend import (
    GitBackend,
    GitBackendError,
    _require_safe_argument,
    _require_safe_ref,
)


def backend() -> GitBackend:
    return GitBackend(GatewaySettings(git_token="tok-123"), http_client=None)


# --- the clone-URL transport allowlist --------------------------------------


def test_local_paths_pass_through():
    assert backend()._clone_url("/tmp/repos/target.git") == "/tmp/repos/target.git"
    assert backend()._clone_url("file:///tmp/x") == "file:///tmp/x"


def test_host_paths_rewrite_to_credentialed_https():
    url = backend()._clone_url("github.com/org/repo")
    assert url == "https://x-access-token:tok-123@github.com/org/repo"
    assert backend()._clone_url("https://github.com/org/repo").startswith(
        "https://x-access-token:"
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
