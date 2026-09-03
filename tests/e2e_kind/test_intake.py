"""Intake, gated on the human, on the live cluster: a goal parks on the
intake gate before the PM ever sees it; one human `intake` signal releases
it; then the operator-deployed PM answers the dispatch, the goal becomes
GitHub issues carrying the PM's summary comment, a `crew_requests` row is
recorded under the PM's own standing identity, the runs row links the
tickets, and the PM's tool calls appear in the audit story under the run's
`run_id`.

The intake gate is the one gate framework path no shipped blueprint
declares, so this module applies a harness variant of `hello-saas.yaml`
that adds `workflows.intake.gates.intake.resolved_by: human`, and restores
the pristine blueprint after. The affirmative PM half rides the same run —
one gated intake proves both.

The denial half ("a PM attempt at sandbox.exec or git.open_pr is denied at
the gateway and audited") is proven by
test_governance.py::test_pm_is_denied_implementer_tools_at_the_gateway.
"""

import asyncio
import re
from pathlib import Path

import pytest
from temporalio.client import Client

from common.ids import new_run_id
from mockllm.brain import PM_SUMMARY
from orchestrator.workflows.base import workflow_id
from orchestrator.workflows.intake import IntakeInput

from .blueprints import (
    HELLO_SAAS,
    apply_blueprint,
    derived_blueprint,
    restore_pristine,
    wait_for_convergence,
)
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


def _intake_gated() -> Path:
    """The pristine example plus the one gate no shipped blueprint carries:
    intake parks on a human before dispatching the PM."""

    def add_gate(document: dict) -> None:
        gates = document["workflows"]["intake"].setdefault("gates", {})
        gates["intake"] = {"resolved_by": "human"}

    return derived_blueprint(HELLO_SAAS, add_gate, "intake-gated")


@pytest.fixture(scope="module")
def gated_intake(stack: None, env: dict) -> dict:
    """Apply the intake-gated blueprint, drive one goal through the gate —
    park, verify nothing dispatched, approve, run to done — and hand back
    every artifact for the assertions. Restores the pristine blueprint
    after, so later suites run against the ungated company."""
    ready = kubectl(
        "-n", COMPANY_NS, "get", "deployment/pm", "-o", "jsonpath={.status.readyReplicas}"
    )
    assert ready == "1", "the PM agent is not deployed/ready"

    run_id = new_run_id()
    goal = (
        "Expose a /stats endpoint summarizing stored slugs "
        f"(e2e intake goal {run_id[-6:].lower()})"
    )
    result: dict = {"env": env, "run_id": run_id}

    async def drive() -> dict:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = await client.start_workflow(
                "intake",
                IntakeInput(run_id=run_id, goal_text=goal, timeouts=deployed_timeouts()),
                id=workflow_id("intake", run_id),
                task_queue=TASK_QUEUE,
            )
            # The gate parks the run before the PM is dispatched.
            for _ in range(240):
                if await handle.query("state") == "paused-intake_gate":
                    break
                await asyncio.sleep(0.25)
            else:
                raise AssertionError("the gated intake never parked on its gate")
            # Captured at the park: the PM's tools have not fired yet, so
            # the gate really sits before the dispatch.
            result["tools_at_park"] = [
                e["tool"] for e in audit_story(run_id) if e["source"] == "gateway"
            ]
            # The one human act releases it.
            await handle.signal("intake", args=[True, "e2e: intake approved"])
            return await asyncio.wait_for(handle.result(), timeout=600)

    # Everything from the apply on is inside try/finally: a failure mid-drive
    # (the park never observed, the result timing out) must still restore the
    # pristine blueprint, or the intake gate stays armed and every later
    # module's intake run parks on a gate nobody answers.
    apply_blueprint(_intake_gated())
    wait_for_convergence()
    try:
        result["record"] = asyncio.run(drive())
        yield result
    finally:
        record = result.get("record") or {}
        match = re.search(r"/issues/(\d+)$", (record.get("ticket_urls") or [""])[0])
        close_issue(env, int(match.group(1)) if match else None)
        with registry() as client:
            client.execute(
                "DELETE FROM registry.crew_requests WHERE run_id = %s", (run_id,)
            )
            client.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))
        restore_pristine()


def test_the_goal_parked_on_the_human_before_any_dispatch(gated_intake: dict):
    """The gate did its one job: the run halted at `paused-intake_gate`
    with no PM tool call yet — no issue created, no crew recorded — so the
    human's approval, not the goal's arrival, is what let the PM run."""
    assert gated_intake["tools_at_park"] == [], gated_intake["tools_at_park"]


def test_the_human_approval_is_a_process_gate_event(gated_intake: dict):
    """The release is on the record like every other gate: a
    `source=process-gate` `gate.intake` event resolved `gate_approved`,
    under the platform lifecycle — a signal answered, not a prompt hoped."""
    gate = [e for e in audit_story(gated_intake["run_id"]) if e["tool"] == "gate.intake"]
    assert any(e["rule"] == "gate_approved" for e in gate), [e["rule"] for e in gate]
    assert all(
        e["source"] == "process-gate" and e["lifecycle"] == "platform" for e in gate
    )


def test_goal_becomes_tickets_and_recorded_intent(gated_intake: dict):
    """Past the gate, the affirmative half: the run is done with tickets
    linked, the ticket is real and carries the PM's summary comment, the
    expiring crew request is recorded under the PM, and the PM's tool calls
    are the run's story — all as the standing pm-1."""
    env = gated_intake["env"]
    run_id = gated_intake["run_id"]
    record = gated_intake["record"]

    assert record["status"] == "done", f"intake failed: {record}"
    assert record["workflow"] == "intake"
    assert record["ticket_urls"], "the runs row links no tickets"

    match = re.search(r"/issues/(\d+)$", record["ticket_urls"][0])
    assert match, record["ticket_urls"]
    issue_number = int(match.group(1))
    issue = github_api(env, "GET", f"/repos/{target_slug(env)}/issues/{issue_number}")
    assert issue.status_code == 200 and issue.json()["state"] == "open"

    comments = github_api(
        env, "GET", f"/repos/{target_slug(env)}/issues/{issue_number}/comments"
    ).json()
    assert [c["body"] for c in comments] == [PM_SUMMARY], comments

    rows = crew_requests(run_id)
    assert len(rows) == 1
    assert rows[0]["conductor"] == "pm-1"
    assert rows[0]["crew_template"] == "feature-delivery"
    assert rows[0]["status"] == "recorded"
    assert rows[0]["expires_at"] is not None

    story = [e for e in audit_story(run_id) if e["source"] == "gateway"]
    tools = [e["tool"] for e in story]
    assert {"issues.create", "crew.request", "issues.comment"} <= set(tools)
    for event in story:
        assert event["agent_id"] == "pm-1"
        assert event["lifecycle"] == "standing"
        assert event["company"] == "hello-saas"
        assert event["decision"] == "allow"
        assert event["workflow"] == "intake"
