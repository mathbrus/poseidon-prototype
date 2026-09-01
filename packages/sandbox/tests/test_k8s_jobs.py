"""The k8s_jobs backend's statically checkable half. Provisioning itself
needs a cluster (the e2e_kind suite); what can be pinned here is the
workspace-preparation command the backend runs after unpacking — the cheap
proxy for the e2e delivery check that a PR diff carries only the agent's
own changes."""

from sandbox.k8s_jobs import _PREPARE_CMD


def test_prepare_keeps_platform_and_build_artifacts_out_of_deliveries():
    """The delivered diff must be the agent's own changes only: the
    platform marker and build artifacts go into git's local exclude, so a
    policy-resolved merge gate's narrow path class is not tripped by
    incidental files."""
    cmd = _PREPARE_CMD.format(workspace="/workspace")
    assert ".git/info/exclude" in cmd
    for pattern in (".poseidon-base", "__pycache__/", "*.pyc"):
        assert pattern in cmd
    # The marker is still written for the tool server to read.
    assert "git rev-parse HEAD > .poseidon-base" in cmd


def test_prepare_strips_every_git_remote_from_the_workspace():
    """Containment, not tidiness: the workspace is a clone of the target
    repo, so it arrives with an `origin` remote — a push/fetch path around
    the gateway and the merge gate. Preparation removes every remote before
    the agent ever runs, so the only way code leaves the sandbox is the
    governed `git.open_pr` tool. (The live e2e that provisioned a real
    sandbox and asserted `git remote` was empty was retired; this pins the
    command that made it true.)"""
    cmd = _PREPARE_CMD.format(workspace="/workspace")
    assert "git remote | xargs -r -n1 git remote remove" in cmd
