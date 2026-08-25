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
