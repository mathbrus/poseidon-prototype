"""The PM's affirmative half, on the live cluster: the operator-deployed
PM answers the intake dispatch, a goal becomes GitHub issues carrying the
PM's summary comment, a `crew_requests` row is recorded under the PM's own
standing identity, the runs row links the tickets, and the PM's tool calls
appear in the audit story under the run's `run_id`. Started exactly as
`poseidon run --workflow intake` starts it — a thin Temporal client; the
in-cluster worker dispatches the deployed PM.

The denial half ("a PM attempt at sandbox.exec or git.open_pr is denied at
the gateway and audited") is proven by
test_governance.py::test_pm_is_denied_executor_tools_at_the_gateway.
"""

import asyncio
import re

from common.ids import new_run_id
from mockllm.brain import PM_SUMMARY
from orchestrator.workflows.base import workflow_id
from orchestrator.workflows.intake import IntakeInput

from .cluster import (
    COMPANY_NS,
    audit_story,
    kubectl,
    registry,
    temporal_port_forward,
)
from .conftest import (
    TASK_QUEUE,
    close_issue,
    crew_requests,
    deployed_timeouts,
    github_api,
    target_slug,
)


def test_goal_becomes_tickets_and_recorded_intent(stack: None, env: dict) -> None:
    from temporalio.client import Client

    # The conductor answering this dispatch is the operator-deployed PM.
    ready = kubectl(
        "-n", COMPANY_NS, "get", "deployment/pm", "-o", "jsonpath={.status.readyReplicas}"
    )
    assert ready == "1", "the PM instance is not deployed/ready"

    run_id = new_run_id()
    goal = (
        "Expose a /stats endpoint summarizing stored slugs "
        f"(e2e intake goal {run_id[-6:].lower()})"
    )

    async def start() -> dict:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = await client.start_workflow(
                "intake",
                IntakeInput(run_id=run_id, goal_text=goal, timeouts=deployed_timeouts()),
                id=workflow_id("intake", run_id),
                task_queue=TASK_QUEUE,
            )
            return await asyncio.wait_for(handle.result(), timeout=600)

    record = asyncio.run(start())
    issue_number = None
    try:
        # The run record is the intake outcome: done, tickets linked.
        assert record["status"] == "done", f"intake failed: {record}"
        assert record["workflow"] == "intake"
        assert record["ticket_urls"], "the runs row links no tickets"

        # The ticket is real.
        match = re.search(r"/issues/(\d+)$", record["ticket_urls"][0])
        assert match, record["ticket_urls"]
        issue_number = int(match.group(1))
        issue = github_api(env, "GET", f"/repos/{target_slug(env)}/issues/{issue_number}")
        assert issue.status_code == 200 and issue.json()["state"] == "open"

        # The PM's finishing paragraph is on the ticket, for whoever picks
        # it up — the mock brain's one-sentence summary, posted by pm-1.
        comments = github_api(
            env, "GET", f"/repos/{target_slug(env)}/issues/{issue_number}/comments"
        ).json()
        assert [c["body"] for c in comments] == [PM_SUMMARY], comments

        # The recorded, expiring request under the PM's authority — the
        # intake -> implement handoff a later implement run consumes.
        rows = crew_requests(run_id)
        assert len(rows) == 1
        assert rows[0]["conductor"] == "pm-1"
        assert rows[0]["crew_template"] == "feature-delivery"
        assert rows[0]["status"] == "recorded"
        assert rows[0]["expires_at"] is not None

        # The PM's tool calls are the run's story.
        story = [e for e in audit_story(run_id) if e["source"] == "gateway"]
        tools = [e["tool"] for e in story]
        assert {"issues.create", "crew.request", "issues.comment"} <= set(tools)
        for event in story:
            assert event["agent_id"] == "pm-1"
            assert event["lifecycle"] == "standing"
            assert event["company"] == "hello-saas"
            assert event["decision"] == "allow"
            assert event["workflow"] == "intake"
    finally:
        if issue_number is not None:
            close_issue(env, issue_number)
        with registry() as client:
            client.execute(
                "DELETE FROM registry.crew_requests WHERE run_id = %s", (run_id,)
            )
            client.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
