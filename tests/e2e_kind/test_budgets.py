"""Budget enforcement, end to end, on kind.

A blueprint edit shrinks hello-saas's per-run cap to a value the mock
brain's second model call must exceed; the LLM gateway hard-stops the run
(`budget_exhausted`, audited, never silent), the implement workflow pauses
into an API-visible `paused-budget_exhausted` state, a rejection signal
finalizes it, the ledger fully attributes the spend, and the budget events
sit in the run's one audit story. And the mirror image: a human who
decides the run is worth more raises the cap (re-applies the real
blueprint) and approves — the workflow re-dispatches with headroom and
runs to a delivered PR. Unattributed model calls are rejected outright.

The module finalizer re-applies the pristine blueprint so later suites run
against the real cap.
"""

import asyncio
import secrets
import time
from typing import Any

import httpx
import pytest
import yaml
from temporalio.client import Client

from common.ids import new_run_id
from orchestrator.workflows.base import Timeouts, workflow_id
from orchestrator.workflows.implement import ImplementInput

from .blueprints import HELLO_SAAS, apply_blueprint, restore_pristine
from .cluster import (
    COMPANY_NS,
    audit_story,
    port_forward,
    projected_token,
    psql_json,
    temporal_port_forward,
)
from .conftest import REPO_ROOT, TASK_QUEUE, close_pr_url, deployed_timeouts

# Below one mock completion's reported cost: the mock prices every
# completion in `x-litellm-response-cost` at $0.002/1K over its
# deterministic token counts, and the implementer's first completion alone
# crosses 50 tokens, so the very first metered call exhausts this cap.
TINY_CAP = 0.0001


@pytest.fixture(scope="module", autouse=True)
def restore_budget(stack: None):
    """Whatever happens here, later suites need the real cap back."""
    yield
    restore_pristine()


@pytest.fixture
def tiny_cap_applied(stack: None, tmp_path_factory) -> None:
    """Function-scoped: every test that needs the tiny cap re-applies it, so
    the approve test's mid-body cap-raise cannot leak the real cap into a
    later test — order stops mattering."""
    document = yaml.safe_load(HELLO_SAAS.read_text())
    document["budgets"]["per_run"]["usd"] = TINY_CAP
    edited = tmp_path_factory.mktemp("budget") / "hello-saas-tiny-cap.yaml"
    edited.write_text(yaml.safe_dump(document, sort_keys=False))
    apply_blueprint(edited)
    # The gateway reads budgets from the registry per call — no rollout to
    # wait for; the operator upsert is complete when apply returns.


def ledger_entries(run_id: str) -> list[dict[str, Any]]:
    return psql_json(
        "SELECT COALESCE(json_agg(l ORDER BY l.ts, l.id), '[]'::json) FROM ("
        "SELECT * FROM registry.budget_ledger WHERE run_id = :'run_id'"
        ") l",
        run_id=run_id,
    )


async def _run_until_paused_then_reject(
    address: str, run_id: str, task_text: str
) -> dict:
    client = await Client.connect(address)
    handle = await client.start_workflow(
        "implement",
        ImplementInput(run_id=run_id, task_text=task_text, timeouts=deployed_timeouts()),
        id=workflow_id("implement", run_id),
        task_queue=TASK_QUEUE,
    )
    # The budget hard-stop leaves an API-visible pending state.
    deadline = time.monotonic() + 300
    while True:
        state = await handle.query("state")
        if state == "paused-budget_exhausted":
            break
        assert time.monotonic() < deadline, f"never paused (state={state!r})"
        await asyncio.sleep(2)
    # The pause *escalates* — a real process-gate audit
    # event fires at half the gate deadline (45s on the fast profile). Wait
    # for it before answering, exactly like the merge-gate e2e does: the
    # escalation path proves itself on every run.
    deadline = time.monotonic() + (deployed_timeouts() or Timeouts()).budget_gate_s - 5
    while True:
        escalated = [
            e
            for e in audit_story(run_id)
            if e["source"] == "process-gate" and e["rule"] == "gate_escalated"
        ]
        if escalated:
            break
        assert time.monotonic() < deadline, (
            "the budget pause never escalated into the audit store"
        )
        await asyncio.sleep(2)
    # A human answers the escalation: no, this run does not get more budget.
    await handle.signal("budget", args=[False, "not worth more spend"])
    return await asyncio.wait_for(handle.result(), timeout=300)


def test_tiny_cap_hard_stops_pauses_and_attributes(tiny_cap_applied, env) -> None:
    run_id = new_run_id()
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text()
    with temporal_port_forward() as address:
        record = asyncio.run(_run_until_paused_then_reject(address, run_id, task_text))

    # The run finalized honestly with the distinguishable budget error.
    assert record["status"] == "failed"
    assert "budget_exhausted" in (record["error"] or "")

    # The ledger fully attributes what was spent before the stop — to the
    # run's ephemeral implementer, the only identity that spoke to a model.
    from orchestrator.identity import ephemeral_agent_id

    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", run_id)
    entries = ledger_entries(run_id)
    assert entries, "no ledger rows for the run"
    for entry in entries:
        # The full attribution tuple, row by row: {run, parent,
        # agent, role} plus company and model.
        assert entry["run_id"] == run_id
        assert entry["company"] == "hello-saas"
        assert entry["agent_id"] == implementer_id
        assert entry["parent_agent_id"] == "pm-1"
        assert entry["role"] == "implementer"
        assert entry["model"] == "mock-brain"
        assert entry["prompt_tokens"] > 0
        # The token breakdown behind the cost reaches Postgres.
        assert int(entry["cached_tokens"]) > 0
    assert sum(float(e["usd"]) for e in entries) >= TINY_CAP

    # Budget events are in the one audit story: metered
    # allows and the hard-stop deny, all source=llm-gateway.
    story = audit_story(run_id)
    llm_events = [e for e in story if e["source"] == "llm-gateway"]
    assert any(
        e["decision"] == "allow" and e["rule"] == "budget_headroom" for e in llm_events
    )
    assert any(
        e["decision"] == "deny" and e["rule"] == "budget_exhausted" for e in llm_events
    ), f"no budget_exhausted deny in the story: {llm_events}"

    # The stop stopped things: no metered model call after the hard-stop —
    # the ledger's spend all precedes the deny in the story's own ordering.
    last_deny = max(
        i
        for i, e in enumerate(story)
        if e["source"] == "llm-gateway" and e["rule"] == "budget_exhausted"
    )
    late_allows = [
        e
        for e in story[last_deny + 1 :]
        if e["source"] == "llm-gateway" and e["decision"] == "allow"
    ]
    assert late_allows == [], f"model calls after the hard-stop: {late_allows}"

    # And the pause is in the story as first-class gate outcomes: the
    # escalation at half-deadline, then the human's rejection — same
    # process-gate contract as the merge gate.
    gate_rules = [
        e["rule"]
        for e in story
        if e["source"] == "process-gate" and e["tool"] == "gate.budget"
    ]
    assert gate_rules == ["gate_escalated", "gate_rejected"], gate_rules
    gate_events = [e for e in story if e["source"] == "process-gate"]
    assert all(e["workflow"] == f"implement-{run_id}" for e in gate_events), gate_events


def test_unattributed_model_call_is_rejected(stack: None) -> None:
    """No run context, no model: with a valid workload identity but
    no run attribution the gateway refuses before any upstream work."""
    token = projected_token(COMPANY_NS, "pm-1")
    with port_forward("svc/llm-gateway", 4000) as port:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "mock-brain", "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "llm_unattributed"

    # And with no credential at all: 401 before anything else.
    with port_forward("svc/llm-gateway", 4000) as port:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/chat/completions",
            json={"model": "mock-brain", "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    assert resp.status_code == 401


async def _run_until_paused_then_approve(
    address: str, run_id: str, task_text: str
) -> tuple[dict, float]:
    """The approval mirror of the rejection driver: park on the budget
    stop, then — as a human who has decided the run is worth it — raise the
    cap by re-applying the real blueprint and approve. The workflow
    re-dispatches with headroom and runs to completion. Returns the terminal
    record and the ledger total captured at the pause, so the caller can
    assert the run spent *more* after resuming — a run that resumed but made
    no further model call would leave that total unchanged."""
    client = await Client.connect(address)
    handle = await client.start_workflow(
        "implement",
        ImplementInput(run_id=run_id, task_text=task_text, timeouts=deployed_timeouts()),
        id=workflow_id("implement", run_id),
        task_queue=TASK_QUEUE,
    )
    deadline = time.monotonic() + 300
    while True:
        state = await handle.query("state")
        if state == "paused-budget_exhausted":
            break
        assert time.monotonic() < deadline, f"never paused (state={state!r})"
        await asyncio.sleep(2)
    spend_at_pause = sum(float(e["usd"]) for e in ledger_entries(run_id))
    # The human raises the cap: re-apply the real blueprint (the gateway
    # reads the cap from the registry per call, so the upsert is enough),
    # then approve — the run may continue.
    await asyncio.to_thread(apply_blueprint, HELLO_SAAS)
    await handle.signal("budget", args=[True, "worth the spend"])
    record = await asyncio.wait_for(handle.result(), timeout=300)
    return record, spend_at_pause


def test_approval_raises_the_cap_and_the_run_completes(tiny_cap_applied, env) -> None:
    """The run that stopped for money is not the end: with the cap raised
    and the human's approval, the workflow re-dispatches and delivers.
    The approval is a `gate.budget` `gate_approved` process-gate event, the
    run finishes `done` with a PR, and the ledger shows spend past the tiny
    cap — real work the first stop had blocked."""
    run_id = new_run_id()
    nonce = secrets.token_hex(3)
    task_text = (REPO_ROOT / "examples" / "task-happy.md").read_text() + (
        f"\n\nDeliver your work on the branch `feature/slugify-{nonce}`.\n"
    )
    record = None
    try:
        with temporal_port_forward() as address:
            record, spend_at_pause = asyncio.run(
                _run_until_paused_then_approve(address, run_id, task_text)
            )
        assert record["status"] == "done", f"the approved run did not finish: {record}"
        assert record["pr_url"], "the approved run delivered no PR"

        gate_rules = [
            e["rule"]
            for e in audit_story(run_id)
            if e["source"] == "process-gate" and e["tool"] == "gate.budget"
        ]
        assert "gate_approved" in gate_rules, gate_rules

        # The run spent *more* after the cap was raised: the pre-pause total
        # already exceeds TINY_CAP by construction, so only a strictly larger
        # final total proves the approval actually resumed real model work.
        final_spend = sum(float(e["usd"]) for e in ledger_entries(run_id))
        assert final_spend > spend_at_pause, (
            f"no spend past the raised cap ({final_spend} == {spend_at_pause}) — "
            "the run never resumed real work"
        )
    finally:
        if record is not None:
            close_pr_url(env, record.get("pr_url"))
