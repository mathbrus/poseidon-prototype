"""The PM on the live cluster: the operator-deployed PM
responds to a dispatch, files a real ticket on the disposable repo, and
records crew intent under its own standing identity — through governed
tools only, no private memory. The denial half ("a PM attempt at
sandbox.exec or git.open_pr is denied at the gateway and audited") is
proven by test_governance.py::test_pm_is_denied_executor_tools_at_the_gateway
and the installed-bundle checks; this file proves the affirmative half.
"""

import contextlib
import re
import socket
import subprocess
import time

import httpx

from common.ids import new_run_id

from .conftest import COMPANY_NS, _psql_json, audit_story
from .test_crew_identity import _registry

PM_LOCAL_PORT = 18002


@contextlib.contextmanager
def _pm_forward():
    proc = subprocess.Popen(
        ["kubectl", "-n", COMPANY_NS, "port-forward", "svc/pm", f"{PM_LOCAL_PORT}:8000"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.monotonic() + 30
        while True:
            try:
                with socket.create_connection(("127.0.0.1", PM_LOCAL_PORT), timeout=1):
                    break
            except OSError as exc:
                if time.monotonic() > deadline:
                    raise RuntimeError("port-forward to the PM never came up") from exc
                time.sleep(0.5)
        yield f"http://127.0.0.1:{PM_LOCAL_PORT}"
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _github(env: dict, method: str, path: str, **kwargs) -> httpx.Response:
    return httpx.request(
        method,
        f"https://api.github.com{path}",
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {env['GIT_TOKEN']}",
        },
        timeout=30,
        **kwargs,
    )


def test_pm_is_deployed_and_scopes_a_goal(stack: None, env: dict) -> None:
    ready = subprocess.run(
        [
            "kubectl",
            "-n",
            COMPANY_NS,
            "get",
            "deployment/pm",
            "-o",
            "jsonpath={.status.readyReplicas}",
        ],
        capture_output=True,
        text=True,
        timeout=30,
    )
    assert ready.stdout.strip() == "1", "the PM instance is not deployed/ready"

    run_id = new_run_id()
    goal = (
        "Add a health-check endpoint returning build metadata "
        f"(e2e pm goal {run_id[-6:].lower()})"
    )

    with _registry() as registry:
        from registry import Runs

        # The dispatching workflow creates the run in production; here the test
        # is the workflow.
        Runs(registry).start(run_id, goal, "intake", None, "hello-saas")
        issue_number = None
        try:
            with _pm_forward() as base:
                resp = httpx.post(
                    f"{base}/task", json={"run_id": run_id, "task_text": goal}, timeout=30
                )
                assert resp.status_code == 202, resp.text
                deadline = time.monotonic() + 300
                while True:
                    status = httpx.get(f"{base}/task/{run_id}", timeout=30).json()
                    if status["status"] != "running":
                        break
                    assert time.monotonic() < deadline, "PM never finished"
                    time.sleep(2)

            assert status["status"] == "done", f"PM failed: {status}"

            # A real ticket on the disposable repo, and recorded crew intent.
            assert status["ticket_urls"], "the PM filed no ticket"
            match = re.search(r"/issues/(\d+)$", status["ticket_urls"][0])
            assert match, status["ticket_urls"]
            issue_number = int(match.group(1))
            slug = "/".join(env["TARGET_REPO"].split("/")[-2:])
            issue = _github(env, "GET", f"/repos/{slug}/issues/{issue_number}")
            assert issue.status_code == 200
            assert issue.json()["state"] == "open"
            assert goal[:40] in issue.json()["title"] + issue.json()["body"]

            assert status["crew_request_id"], "no crew intent recorded"
            rows = _psql_json(
                "SELECT COALESCE(json_agg(c), '[]'::json) FROM ("
                "SELECT * FROM registry.crew_requests WHERE run_id = :'run_id'"
                ") c",
                run_id,
                default="[]",
            )
            assert len(rows) == 1
            assert rows[0]["conductor"] == "pm-1"
            assert rows[0]["crew_template"] == "feature-delivery"
            assert rows[0]["status"] == "recorded"

            # Every PM decision in the story, as the standing pm-1.
            story = [e for e in audit_story(run_id) if e["source"] == "gateway"]
            tools = [e["tool"] for e in story]
            assert "issues.create" in tools and "crew.request" in tools
            for event in story:
                assert event["agent_id"] == "pm-1"
                assert event["lifecycle"] == "standing"
                assert event["company"] == "hello-saas"
                assert event["decision"] == "allow"
        finally:
            if issue_number is not None:
                slug = "/".join(env["TARGET_REPO"].split("/")[-2:])
                with contextlib.suppress(Exception):
                    _github(
                        env,
                        "PATCH",
                        f"/repos/{slug}/issues/{issue_number}",
                        json={"state": "closed"},
                    )
            registry.execute(
                "DELETE FROM registry.crew_requests WHERE run_id = %s", (run_id,)
            )
            registry.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
