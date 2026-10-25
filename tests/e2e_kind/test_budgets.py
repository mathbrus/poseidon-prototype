"""Budget enforcement, end to end, on kind.

A blueprint edit shrinks hello-saas's per-run cap to a value the run's
second implementer call must exceed; the LLM gateway refuses the run
the heartbeat started (`budget_exhausted`, audited, never silent), the
implement workflow parks into an API-visible `parked-budget_exhausted`
state, a person's rejection finalizes it, the ledger fully attributes the
spend, and the budget events sit in the run's one audit story. And the
mirror image: a human who decides the run is worth more raises the cap
(re-applies the real blueprint) and approves — the workflow re-dispatches
with headroom and runs to a delivered PR. That a park escalates at half
its deadline is shown on the merge gate (`test_split_oracle_redteam`'s
laundered scenario) and in the run workflow's own tests; an unattributed
model call is the LLM gateway's own test.

The tiny-cap blueprint is the one-shot company (`max_attempts: 1`): a run the
human rejects is the company's last word on its ticket, not a retry the
loop would start on its own. The module finalizer re-applies the pristine
blueprint so later suites run against the real cap.
"""

import secrets

import pytest

from common.audit import gate_event
from common.console import SpendEntryView
from common.tasks import issue_run_task
from common.workflows import BUDGET_GATE

from .blueprints import (
    derived_blueprint,
    pristine_blueprint,
    restore_pristine,
    single_attempt,
)
from .cluster import (
    audit_story,
    await_run,
    console,
    run_by_task,
)
from .conftest import (
    apply_and_settle,
    await_gate,
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

    return derived_blueprint(single_attempt(pristine_blueprint()), shrink, "tiny-cap")


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
    apply_and_settle(_tiny_cap(), env)


def ledger_entries(run_id: str) -> list[SpendEntryView]:
    """Every model call attributed to the run, as the console reads them."""
    return console().spend(run_id).entries


def _parked_run(env: dict) -> tuple[int, str]:
    """File the happy ticket, let the heartbeat start it, and wait for the
    API-visible pending state its cap's refusal parks it in. Returns the ticket and
    the run the loop minted."""
    nonce = secrets.token_hex(3)
    number = file_ticket(env, f"happy ({nonce})", ticket_body("happy", nonce))
    wake_until_run_started(issue_run_task(number), "implement")
    run_id = run_by_task(issue_run_task(number), workflow="implement")["run_id"]

    await_gate(run_id, "parked-budget_exhausted", deadline_s=300)
    return number, run_id


def _signal_budget(run_id: str, approved: bool, reason: str) -> None:
    """The human's answer to the budget park, through the console: the
    verdict and the reason are recorded under their name, then delivered."""
    console().resolve(run_id, "budget", approved, reason)


def test_tiny_cap_refuses_parks_and_attributes(tiny_cap_applied, env) -> None:
    number, run_id = _parked_run(env)
    try:
        # A human answers the park: no, this run does not get more budget.
        _signal_budget(run_id, False, "not worth more spend")
        record = await_run(issue_run_task(number), terminal=True, deadline_s=300)
    finally:
        close_issue(env, number)

    # The run finalized honestly with the distinguishable budget error.
    assert record["status"] == "failed"
    assert "budget_exhausted" in (record["error"] or "")

    # The ledger fully attributes what was spent before the refusal: the
    # conductor's decision that chose this run, then the run's ephemeral
    # implementer — the only two identities that spoke to a model.
    from orchestrator.identity import ephemeral_agent_id

    implementer_id = ephemeral_agent_id("feature-delivery", "implementer", run_id)
    entries = ledger_entries(run_id)
    assert [e.agent_id for e in entries[:2]] == ["pm-1", implementer_id], entries
    for entry in entries:
        assert entry.run_id == run_id
        assert entry.model == "mock-brain"
        assert entry.prompt_tokens > 0
        if entry.agent_id == "pm-1":
            assert entry.role == "pm"
        else:
            assert entry.agent_id == implementer_id
            assert entry.parent_agent_id == "pm-1"
            assert entry.role == "implementer"
            assert entry.cached_tokens > 0
    assert sum(e.usd for e in entries) >= TINY_CAP

    # Budget events are in the one audit story: metered allows and the
    # cap's deny, all source=llm-gateway.
    story = audit_story(run_id)
    llm_events = [e for e in story if e["source"] == "llm-gateway"]
    assert any(
        e["decision"] == "allow" and e["rule"] == "budget_headroom" for e in llm_events
    )
    assert any(
        e["decision"] == "deny" and e["rule"] == "budget_exhausted" for e in llm_events
    ), f"no budget_exhausted deny in the story: {llm_events}"

    # The refusal held: no metered model call after the cap's deny.
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
    assert late_allows == [], f"model calls after the cap's deny: {late_allows}"

    # And the park is in the story as first-class gate outcomes: the park
    # itself, then the human's rejection — same process-gate contract as the
    # merge gate.
    gate_rules = [
        e["rule"]
        for e in story
        if e["source"] == "process-gate" and e["tool"] == gate_event(BUDGET_GATE)
    ]
    assert gate_rules == ["gate_parked", "gate_rejected"], gate_rules
    gate_events = [e for e in story if e["source"] == "process-gate"]
    assert all(e["workflow"] == f"implement-{run_id}" for e in gate_events), gate_events


def test_a_raised_cap_and_an_approving_verdict_complete_the_run(
    tiny_cap_applied, env
) -> None:
    """The run that stopped for money is not the end: with the cap raised
    and the human's approving verdict, the workflow re-dispatches and
    delivers. The verdict is a `gate.budget` `gate_approved` process-gate event, the
    run finishes `done` with a PR, and the ledger shows spend past the tiny
    cap — real work the first stop had blocked."""
    number, run_id = _parked_run(env)
    record = None
    try:
        spend_at_park = sum(e.usd for e in ledger_entries(run_id))
        # The human raises the cap: re-apply the real blueprint (the gateway
        # reads the cap from the registry per call, so the upsert is enough),
        # then approve — the run may continue.
        from .blueprints import apply_blueprint

        apply_blueprint(single_attempt(pristine_blueprint()))
        _signal_budget(run_id, True, "worth the spend")
        record = await_run(issue_run_task(number), terminal=True, deadline_s=600)
        assert record["status"] == "done", f"the approved run did not finish: {record}"
        assert record["pr_url"], "the approved run delivered no PR"

        gate_rules = [
            e["rule"]
            for e in audit_story(run_id)
            if e["source"] == "process-gate" and e["tool"] == gate_event(BUDGET_GATE)
        ]
        assert "gate_approved" in gate_rules, gate_rules

        # The run spent *more* after the cap was raised: the pre-park total
        # already exceeds TINY_CAP by construction, so only a strictly larger
        # final total proves the verdict actually resumed real model work.
        final_spend = sum(e.usd for e in ledger_entries(run_id))
        assert final_spend > spend_at_park, (
            f"no spend past the raised cap ({final_spend} == {spend_at_park}) — "
            "the run never resumed real work"
        )
    finally:
        if record is not None:
            close_pr_url(env, record.get("pr_url"))
        close_issue(env, number)
