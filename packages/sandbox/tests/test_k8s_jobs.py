"""The k8s_jobs backend's statically checkable half. Provisioning itself
needs a cluster (the e2e_kind suite); what can be run here is the
workspace-preparation command the backend runs after unpacking, on a
workspace shaped like the clone it unpacks — the cheap proxy for the e2e
delivery check that a pull request's diff carries only the agent's own
changes."""

from pathlib import Path

from conftest import cloned, git, prepare


def test_prepare_keeps_platform_and_build_artifacts_out_of_deliveries(
    workspace: Path,
):
    """The delivered diff must be the agent's own changes only: the
    platform marker and build artifacts go into git's local exclude, so an
    automated merge gate's narrow automated paths are not tripped by
    incidental files."""
    (workspace / "pkg" / "__pycache__").mkdir()
    (workspace / "pkg" / "__pycache__" / "mod.cpython-312.pyc").write_bytes(b"\0")
    (workspace / "stray.pyc").write_bytes(b"\0")
    assert git(workspace, "status", "--porcelain", "--untracked-files=all") == ""
    # The marker is still written, for the tool server to read.
    marker = (workspace / ".poseidon-base").read_text()
    assert marker == git(workspace, "rev-parse", "HEAD") + "\n"


def test_prepare_strips_every_git_remote_from_the_workspace(tmp_path: Path):
    """Containment, not tidiness: the workspace is a clone of the target
    repo, so it arrives with an `origin` remote — a push/fetch path around
    the gateway and the merge gate. Preparation removes every remote before
    the agent ever runs, so the only way code leaves the sandbox is the
    governed `git.open_pr` tool."""
    clone = cloned(tmp_path)
    assert git(clone, "remote").split() == ["origin", "upstream"]
    assert git(prepare(clone), "remote") == ""
