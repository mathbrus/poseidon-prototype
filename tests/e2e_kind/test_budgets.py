"""Budget enforcement, end to end, on kind.

A blueprint edit shrinks hello-saas's per-run cap to a value the run's
second implementer call must exceed; the LLM gateway hard-stops the run
the heartbeat started (`budget_exhausted`, audited, never silent), the
implement workflow pauses into an API-visible `paused-budget_exhausted`
state, a rejection signal finalizes it, the ledger fully attributes the
spend, and the budget events sit in the run's one audit story. And the
mirror image: a human who decides the run is worth more raises the cap
(re-applies the real blueprint) and approves — the workflow re-dispatches
with headroom and runs to a delivered PR. Unattributed model calls are
rejected outright.

The tiny-cap profile is the one-shot company (`max_attempts: 1`): a run the
human rejects is the company's last word on its ticket, not a retry the
loop would start on its own. The module finalizer re-applies the pristine
profile so later suites run against the real cap.
"""

import asyncio
import secrets
import time
from typing import Any

import httpx
import pytest
from temporalio.client import Client

from common.tasks import issue_run_task
from orchestrator.workflows.base import workflow_id

from .blueprints import (
    E2E_PAUSE_MINUTES,
    derived_blueprint,
    pristine_profile,
    restore_pristine,
    single_attempt,
)
from .cluster import (
    COMPANY_NS,
    audit_story,
    await_run,
    port_forward,
    projected_token,
    psql_json,
    run_by_task,
    temporal_port_forward,
)
from .conftest import (
    apply_profile,
    close_issue,
    close_pr_url,
    file_ticket,
    ticket_body,
    wake_until_run_started,
)

# The mock prices every completion in `x-litellm-response-cost` at
# $0.002/1K over its deterministic token counts. The heartbeat's own
# decision is metered under the run it chose (about a thousand prompt
# tokens, $0.002), and the implementer's first completion costs about twice
# that: the decision and one implementer call fit under this cap, the
# second implementer call finds no headroom and is refused.
TINY_CAP = 0.003


def _tiny_cap():
    def shrink(document: dict) -> None:
        document["budgets"]["per_run"]["usd"] = TINY_CAP

    return derived_blueprint(single_attempt(pristine_profile()), shrink, "tiny-cap")


@pytest.fixture(scope="module", autouse=True)
def restore_budget(stack: None):
    """Whatever happens here, later suites need the real cap back."""
    yield
    restore_pristine()


@pytest.fixture
def tiny_cap_applied(stack: None, env: dict) -> None:
    """Function-scoped: every test that needs the tiny cap re-applies it, so
    the approve test's mid-body cap-raise cannot leak the real cap into a
    later test — order stops mattering."""
    apply_profile(_tiny_cap(), env)


def ledger_entries(run_id: str) -> list[dict[str, Any]]:
    return psql_json(
        "SELECT COALESCE(json_agg(l ORDER BY l.ts, l.id), '[]'::json) FROM ("
        "SELECT * FROM registry.budget_ledger WHERE run_id = :'run_id'"
        ") l",
        run_id=run_id,
    )


def _paused_run(env: dict) -> tuple[int, str]:
    """File the happy ticket, let the heartbeat start it, and wait for the
    budget hard-stop's API-visible pending state. Returns the ticket and
    the run the loop minted."""
    nonce = secrets.token_hex(3)
    number = file_ticket(env, f"happy ({nonce})", ticket_body("happy", nonce))
    wake_until_run_started(issue_run_task(number), "implement")
    run_id = run_by_task(issue_run_task(number), workflow="implement")["run_id"]

    async def wait_for_pause() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(workflow_id("implement", run_id))
            deadline = time.monotonic() + 300
            while True:
                state = await handle.query("state")
                if state == "paused-budget_exhausted":
                    return
                assert time.monotonic() < deadline, f"never paused (state={state!r})"
                await asyncio.sleep(2)

    asyncio.run(wait_for_pause())
    return number, run_id


def _signal_budget(run_id: str, approved: bool, payload: str) -> None:
    async def send() -> None:
        with temporal_port_forward() as address:
            client = await Client.connect(address)
            handle = client.get_workflow_handle(workflow_id("implement", run_id))
            await handle.signal("budget", args=[approved, payload])

    asyncio.run(send())


def test_tiny_cap_hard_stops_pauses_and_attributes(tiny_cap_applied, env) -> None:
    number, run_id = _paused_run(env)
    try:
        # The pause *escalates* — a real process-gate audit event fires at
        # half the declared pause. Wait for it before answering, exactly like
        # the merge-gate e2e does: the escalation path proves itself on
        # every run.
        deadline = time.monotonic() + E2E_PAUSE_MINUTES * 60 - 5
        while not any(
            e["source"] == "process-gate" and e["rule"] == "gate_escalated"
            for e in audit_story(run_id)
        ):
            assert time.monotonic() < deadline, (
                "the budget pause never escalated into the audit store"
            )
            time.sleep(2)
        # A human answers the escalation: no, this run does not get more budget.
        _signal_budget(run_id, False, "not worth more spend")
        record = await_run(issue_run_task(number), terminal=True, deadline_s=300)
    finally:
        close_issue(env, number)

    # The run finalized honestly with the distinguishable budget error.
    assert record["status"] == "failed"
    assert "budget_exhausted" in (record["error"] or "")

    # The ledger fully attributes what was spent before the stop: the
    # conductor's decision that chose this run, then the run's ephemeral
    # implementer — the only two identities that spoke to a model.
    from orchestrator.identity import ephemeral_agent_id

    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", run_id)
    entries = ledger_entries(run_id)
    assert [e["agent_id"] for e in entries[:2]] == ["pm-1", implementer_id], entries
    for entry in entries:
        assert entry["run_id"] == run_id
        assert entry["company"] == "hello-saas"
        assert entry["model"] == "mock-brain"
        assert entry["prompt_tokens"] > 0
        if entry["agent_id"] == "pm-1":
            assert entry["role"] == "pm"
        else:
            assert entry["agent_id"] == implementer_id
            assert entry["parent_agent_id"] == "pm-1"
            assert entry["role"] == "implementer"
            assert int(entry["cached_tokens"]) > 0
    assert sum(float(e["usd"]) for e in entries) >= TINY_CAP

    # Budget events are in the one audit story: metered allows and the
    # hard-stop deny, all source=llm-gateway.
    story = audit_story(run_id)
    llm_events = [e for e in story if e["source"] == "llm-gateway"]
    assert any(
        e["decision"] == "allow" and e["rule"] == "budget_headroom" for e in llm_events
    )
    assert any(
        e["decision"] == "deny" and e["rule"] == "budget_exhausted" for e in llm_events
    ), f"no budget_exhausted deny in the story: {llm_events}"

    # The stop stopped things: no metered model call after the hard-stop.
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

    with port_forward("svc/llm-gateway", 4000) as port:
        resp = httpx.post(
            f"http://127.0.0.1:{port}/chat/completions",
            json={"model": "mock-brain", "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    assert resp.status_code == 401


def test_approval_raises_the_cap_and_the_run_completes(tiny_cap_applied, env) -> None:
    """The run that stopped for money is not the end: with the cap raised
    and the human's approval, the workflow re-dispatches and delivers.
    The approval is a `gate.budget` `gate_approved` process-gate event, the
    run finishes `done` with a PR, and the ledger shows spend past the tiny
    cap — real work the first stop had blocked."""
    number, run_id = _paused_run(env)
    record = None
    try:
        spend_at_pause = sum(float(e["usd"]) for e in ledger_entries(run_id))
        # The human raises the cap: re-apply the real blueprint (the gateway
        # reads the cap from the registry per call, so the upsert is enough),
        # then approve — the run may continue.
        from .blueprints import apply_blueprint

        apply_blueprint(single_attempt(pristine_profile()))
        _signal_budget(run_id, True, "worth the spend")
        record = await_run(issue_run_task(number), terminal=True, deadline_s=600)
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
        close_issue(env, number)
