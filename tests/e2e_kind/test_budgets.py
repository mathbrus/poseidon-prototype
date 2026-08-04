"""The §4.8 exit criteria on kind (plan step 14).

A blueprint edit shrinks hello-saas's per-run cap to a value the mock
brain's second model call must exceed; the LLM gateway hard-stops the run
(`budget_exhausted`, audited, never silent), the implement workflow pauses
into an API-visible `paused-budget_exhausted` state, a rejection signal
finalizes it, the ledger fully attributes the spend, and the budget events
sit in the run's one audit story. Unattributed model calls are rejected
outright.

The module finalizer re-applies the pristine blueprint so later suites run
against the real cap.
"""

import asyncio
import subprocess
import time
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml
from temporalio.client import Client

from common.ids import new_run_id
from orchestrator.workflows.implement import (
    FAST_TIMEOUTS,
    ImplementInput,
    workflow_id,
)

from .conftest import (
    COMPANY_NS,
    PLATFORM_NS,
    REPO_ROOT,
    TASK_QUEUE,
    _psql_json,
    audit_story,
    temporal_port_forward,
)
from .test_operator_lifecycle import (
    HELLO_SAAS,
    apply_blueprint,
    port_forward,
    wait_for_convergence,
)
from .test_registry_durability import projected_token

LLM_LOCAL_PORT = 14001
TINY_CAP = 0.0001  # below one mock completion's cost at $0.002/1K tokens


@pytest.fixture(scope="module", autouse=True)
def restore_budget(stack: None):
    """Whatever happens here, later suites need the real cap back."""
    yield
    apply_blueprint(HELLO_SAAS)
    wait_for_convergence()


@pytest.fixture(scope="module")
def tiny_cap_applied(stack: None, tmp_path_factory) -> None:
    document = yaml.safe_load(HELLO_SAAS.read_text())
    document["budgets"]["per_run"]["usd"] = TINY_CAP
    edited = tmp_path_factory.mktemp("budget") / "hello-saas-tiny-cap.yaml"
    edited.write_text(yaml.safe_dump(document, sort_keys=False))
    apply_blueprint(edited)
    # The gateway reads budgets from the registry per call — no rollout to
    # wait for; the operator upsert is complete when apply returns.


def ledger_entries(run_id: str) -> list[dict[str, Any]]:
    return _psql_json(
        "SELECT COALESCE(json_agg(l ORDER BY l.ts, l.id), '[]'::json) FROM ("
        "SELECT * FROM registry.budget_ledger WHERE run_id = :'run_id'"
        ") l",
        run_id,
        default="[]",
    )


async def _run_until_paused_then_reject(address: str, run_id: str, task_text: str) -> dict:
    client = await Client.connect(address)
    handle = await client.start_workflow(
        "implement",
        ImplementInput(run_id=run_id, task_text=task_text, timeouts=FAST_TIMEOUTS),
        id=workflow_id(run_id),
        task_queue=TASK_QUEUE,
    )
    # The §4.8 promise: the stop is an API-visible pending state.
    deadline = time.monotonic() + 300
    while True:
        state = await handle.query("state")
        if state == "paused-budget_exhausted":
            break
        assert time.monotonic() < deadline, f"never paused (state={state!r})"
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

    # The ledger fully attributes what was spent before the stop.
    entries = ledger_entries(run_id)
    assert entries, "no ledger rows for the run"
    for entry in entries:
        assert entry["company"] == "hello-saas"
        assert entry["agent_id"] == "backend-engineer-1"
        assert entry["role"] == "backend-engineer"
        assert entry["model"] == "mock-brain"
        assert entry["prompt_tokens"] > 0
    assert sum(float(e["usd"]) for e in entries) >= TINY_CAP

    # Budget events are in the one audit story (steps 13+14): metered
    # allows and the hard-stop deny, all source=llm-gateway.
    story = audit_story(run_id)
    llm_events = [e for e in story if e["source"] == "llm-gateway"]
    assert any(
        e["decision"] == "allow" and e["rule"] == "budget_headroom"
        for e in llm_events
    )
    assert any(
        e["decision"] == "deny" and e["rule"] == "budget_exhausted"
        for e in llm_events
    ), f"no budget_exhausted deny in the story: {llm_events}"


def test_unattributed_model_call_is_rejected(stack: None) -> None:
    """§4.8: no run context, no model — with a valid workload identity but
    no run attribution the gateway refuses before any upstream work."""
    token = projected_token(COMPANY_NS, "backend-engineer-1")
    with port_forward("llm-gateway", LLM_LOCAL_PORT, 4000) as base:
        resp = httpx.post(
            f"{base}/chat/completions",
            headers={"Authorization": f"Bearer {token}"},
            json={"model": "mock-brain",
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    assert resp.status_code == 400
    assert resp.json()["error"]["code"] == "llm_unattributed"

    # And with no credential at all: 401 before anything else.
    with port_forward("llm-gateway", LLM_LOCAL_PORT, 4000) as base:
        resp = httpx.post(
            f"{base}/chat/completions",
            json={"model": "mock-brain",
                  "messages": [{"role": "user", "content": "hi"}]},
            timeout=15,
        )
    assert resp.status_code == 401
