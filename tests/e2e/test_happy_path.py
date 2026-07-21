"""E1 — happy path: the agent iterates in a sandbox and a real PR appears
on the git host, opened by the gateway on a `feature/*` branch."""

import re

from runner.flow import Manifest


def test_run_completes_with_pr(happy_run: Manifest) -> None:
    assert happy_run.status == "done", f"run failed: {happy_run.error}"
    assert happy_run.pr_url, "no PR URL in the run manifest"
    assert happy_run.iterations >= 1
    assert happy_run.audit_lines > 0


def test_pr_is_on_feature_branch(happy_run: Manifest, pr_info) -> None:
    pr = pr_info(happy_run.pr_url)
    assert pr["state"] == "open"
    assert re.fullmatch(r"feature/[a-z0-9-]+", pr["head"]["ref"]), (
        f"PR branch is {pr['head']['ref']!r}, not feature/*"
    )
