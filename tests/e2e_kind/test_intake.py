"""Intake on the live cluster: a goal becomes GitHub issues
plus a recorded `crew_requests` row under the PM's authority; the runs row
links the tickets; and the PM's tool calls appear in the audit story under
the run's `run_id`. Started exactly as `poseidon run --workflow intake`
starts it — a thin Temporal client; the in-cluster worker drives the PM.
"""

import asyncio
import contextlib
import re

from common.ids import new_run_id
from orchestrator.workflows.intake import IntakeInput, intake_workflow_id

from .conftest import (
    TASK_QUEUE,
    _psql_json,
    audit_story,
    deployed_timeouts,
    github_api,
    temporal_port_forward,
)
from .test_crew_identity import _registry


def test_goal_becomes_tickets_and_recorded_intent(stack: None, env: dict) -> None:
    from temporalio.client import Client

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
                id=intake_workflow_id(run_id),
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
        slug = "/".join(env["TARGET_REPO"].split("/")[-2:])
        issue = github_api(env, "GET", f"/repos/{slug}/issues/{issue_number}")
        assert issue.status_code == 200 and issue.json()["state"] == "open"

        # The recorded, expiring request under the PM's authority — the
        # intake -> implement handoff a later implement run consumes.
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
        assert rows[0]["expires_at"] is not None

        # The PM's tool calls are the run's story.
        story = [e for e in audit_story(run_id) if e["source"] == "gateway"]
        tools = [e["tool"] for e in story]
        assert "issues.create" in tools and "crew.request" in tools
        for event in story:
            assert event["agent_id"] == "pm-1"
            assert event["lifecycle"] == "standing"
            assert event["workflow"] == "intake"
    finally:
        if issue_number is not None:
            slug = "/".join(env["TARGET_REPO"].split("/")[-2:])
            with contextlib.suppress(Exception):
                github_api(
                    env,
                    "PATCH",
                    f"/repos/{slug}/issues/{issue_number}",
                    json={"state": "closed"},
                )
        with _registry() as registry:
            registry.execute(
                "DELETE FROM registry.crew_requests WHERE run_id = %s", (run_id,)
            )
            registry.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
