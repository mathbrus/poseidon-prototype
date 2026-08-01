"""E1 on kind (plan step 1d): the agent iterates in a sandbox and a real PR
appears on the git host, opened by the gateway on a `feature/*` branch — the
same happy path as the Compose E1, against the re-hosted cluster.

The audit assertion reads the gateway's PVC-backed JSONL through kubectl:
the manifest's host-side `audit_lines` count is always 0 on kind (the file
no longer lives on the host; the registry sink replaces it at plan step 8).
"""

import re

from runner.flow import Manifest

from .conftest import cluster_audit_events


def test_run_completes_with_pr(happy_run: Manifest) -> None:
    assert happy_run.status == "done", f"run failed: {happy_run.error}"
    assert happy_run.pr_url, "no PR URL in the run manifest"
    assert happy_run.iterations >= 1


def test_audit_story_on_cluster(happy_run: Manifest) -> None:
    events = cluster_audit_events(happy_run.run_id)
    assert events, "no audit events for the run in the gateway's PVC JSONL"


def test_pr_is_on_feature_branch(happy_run: Manifest, pr_info) -> None:
    pr = pr_info(happy_run.pr_url)
    assert pr["state"] == "open"
    assert re.fullmatch(r"feature/[a-z0-9-]+", pr["head"]["ref"]), (
        f"PR branch is {pr['head']['ref']!r}, not feature/*"
    )
