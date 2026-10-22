"""What a human may do now: the preview and the act answer alike.

The guarantee is parity. For every act the previews list, in every state
of the fixture below and for a human the company names and one it does
not, the preview's answer is what the act itself answers when asked:
allowed is a 200, a policy refusal is the 403 naming the same rule, a
state refusal is the 422 with the same detail. They share one policy
input and one state check per act (`console.acts`), so they cannot drift;
this test is what says they did not.

And a preview records nothing and does nothing.
"""

from collections.abc import Callable
from typing import Any

import httpx
import pytest

from common.console import ActList, ActView
from common.policy import (
    BLUEPRINT_APPLY,
    COMPANY_DELETE,
    GATE_RESOLVE,
    HEARTBEAT_PAUSE,
    HEARTBEAT_RESUME,
    HEARTBEAT_WAKE,
    RUN_CANCEL,
    RUN_START,
)
from common.tasks import review_run_task
from common.workflows import (
    BUDGET_GATE,
    HEARTBEAT_PAUSED,
    HEARTBEAT_SLEEPING,
    MERGE_GATE,
    PARKED_STATE,
    RUN_RUNNING,
    RUN_STATUS_DONE,
    workflow_id,
)
from console.fakes import AUDITOR_TOKEN, COMPANY, HELLO_SAAS, OWNER_TOKEN, World

pytestmark = pytest.mark.anyio

RUN = "run-01PARITY"
REVIEW_WF = workflow_id("review", RUN)
HEARTBEAT_WF = workflow_id("heartbeat", COMPANY)


def a_review(world: World, state: str | None) -> None:
    """A review run whose workflow answers `state`, or has ended (None)."""
    world.runs.start(RUN, review_run_task(12), "review", REVIEW_WF, COMPANY)
    if state is None:
        world.runs.finish(RUN, RUN_STATUS_DONE)
    else:
        world.workflows.running[REVIEW_WF] = state


def resting(world: World) -> None:
    world.workflows.running[HEARTBEAT_WF] = HEARTBEAT_SLEEPING
    a_review(world, PARKED_STATE[MERGE_GATE])


def held(world: World) -> None:
    world.workflows.running[HEARTBEAT_WF] = HEARTBEAT_PAUSED
    world.companies.pause(COMPANY, "owner")
    a_review(world, RUN_RUNNING)


def over(world: World) -> None:
    """No heartbeat running yet, and the review's workflow gone."""
    a_review(world, None)


def on_budget(world: World) -> None:
    world.workflows.running[HEARTBEAT_WF] = HEARTBEAT_SLEEPING
    a_review(world, PARKED_STATE[BUDGET_GATE])


SCENARIOS: dict[str, Callable[[World], None]] = {
    "resting": resting,
    "held": held,
    "over": over,
    "on_budget": on_budget,
}


def a_world(scenario: Callable[[World], None], monkeypatch) -> World:
    world = World()
    world.humans = "owner,auditor"
    world.patch(monkeypatch.setattr)
    scenario(world)
    return world


def request_for(view: ActView) -> tuple[str, str, dict[str, Any] | None]:
    """The act a preview stands for, as a client would ask for it."""
    args = view.args
    why = {"rationale": "parity"}
    return {
        BLUEPRINT_APPLY: lambda: (
            "POST",
            "/blueprint/apply",
            {"blueprint": HELLO_SAAS.read_text()},
        ),
        COMPANY_DELETE: lambda: ("DELETE", "/company", {**why, "wait": False}),
        HEARTBEAT_WAKE: lambda: ("POST", "/heartbeat/wake", None),
        HEARTBEAT_PAUSE: lambda: ("POST", "/heartbeat/pause", why),
        HEARTBEAT_RESUME: lambda: ("POST", "/heartbeat/resume", why),
        RUN_START: lambda: (
            "POST",
            "/runs",
            {"workflow": args.get("workflow"), "number": 99},
        ),
        RUN_CANCEL: lambda: ("POST", f"/runs/{RUN}/cancel", why),
        GATE_RESOLVE: lambda: (
            "POST",
            f"/runs/{RUN}/gates/{args.get('gate')}",
            {"approved": args.get("approved"), **why},
        ),
    }[view.act]()


async def previews(world: World, token: str) -> list[ActView]:
    headers = {"Authorization": f"Bearer {token}"}
    async with world.client() as client:
        company = await client.get("/acts", headers=headers)
        run = await client.get(f"/runs/{RUN}/acts", headers=headers)
    assert company.status_code == run.status_code == 200, (company.text, run.text)
    return (
        ActList.model_validate(company.json()).acts
        + ActList.model_validate(run.json()).acts
    )


@pytest.mark.parametrize("scenario", SCENARIOS)
@pytest.mark.parametrize(
    "token", [OWNER_TOKEN, AUDITOR_TOKEN], ids=["named", "not_named"]
)
async def test_every_preview_answers_as_its_act_does(scenario, token, monkeypatch):
    listed = await previews(a_world(SCENARIOS[scenario], monkeypatch), token)
    assert {view.act for view in listed} == {
        BLUEPRINT_APPLY,
        COMPANY_DELETE,
        HEARTBEAT_WAKE,
        HEARTBEAT_PAUSE,
        HEARTBEAT_RESUME,
        RUN_START,
        RUN_CANCEL,
        GATE_RESOLVE,
    }
    for view in listed:
        # Each act in a world of its own: an act changes what the next sees.
        world = a_world(SCENARIOS[scenario], monkeypatch)
        method, path, body = request_for(view)
        async with world.client() as client:
            answer = await client.request(
                method, path, json=body, headers={"Authorization": f"Bearer {token}"}
            )
        what = (scenario, token, view.act, view.args, answer.text)
        if view.allowed:
            assert answer.status_code == 200, what
        elif view.refusal.kind == "policy":
            assert answer.status_code == 403, what
            assert answer.json()["rule"] == view.refusal.rule, what
        else:
            assert answer.status_code == 422, what
            assert answer.json()["detail"] == view.refusal.detail, what


async def test_a_preview_records_nothing_and_does_nothing(monkeypatch):
    world = a_world(resting, monkeypatch)
    await previews(world, OWNER_TOKEN)
    await previews(world, AUDITOR_TOKEN)
    assert world.audit.events == []
    assert world.workflows.calls == [] and world.submitted == [] and world.deleted == []
    assert world.companies.paused(COMPANY) is None


async def test_a_named_human_on_a_parked_run_may_answer_its_gate(monkeypatch):
    listed = {
        (view.act, view.args.get("gate"), view.args.get("approved")): view
        for view in await previews(a_world(resting, monkeypatch), OWNER_TOKEN)
    }
    assert listed[(GATE_RESOLVE, "merge", True)].allowed
    budget = listed[(GATE_RESOLVE, "budget", True)]
    assert budget.refusal.kind == "state" and "not parked" in budget.refusal.detail
    resume = listed[(HEARTBEAT_RESUME, None, None)]
    assert resume.refusal.detail == f"{COMPANY!r} is not paused"
    assert listed[(HEARTBEAT_PAUSE, None, None)].rationale_required
    assert not listed[(HEARTBEAT_WAKE, None, None)].rationale_required


async def test_a_human_the_company_does_not_name_is_refused_by_policy(monkeypatch):
    """Where the state would let anyone act, policy refuses the person the
    company does not name — by the rule that says so."""
    for view in await previews(a_world(resting, monkeypatch), AUDITOR_TOKEN):
        assert not view.allowed, view
        if view.refusal.kind == "policy":
            assert view.refusal.rule == "human_not_recognized", view


async def test_the_previews_ask_policy_all_at_once(monkeypatch):
    """One preview, many questions: OPA is asked concurrently, not one
    question after another."""
    world = a_world(resting, monkeypatch)
    in_flight = peak = 0
    answer = world.evaluate

    async def counting(opa_url: str, company: str, decision_input: dict) -> dict:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            import asyncio

            await asyncio.sleep(0.01)
            return await answer(opa_url, company, decision_input)
        finally:
            in_flight -= 1

    from console import service as service_module

    monkeypatch.setattr(service_module, "evaluate_decision", counting)
    async with world.client() as client:
        listed = await client.get(
            "/acts", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
        )
    assert listed.status_code == 200
    assert peak > 1


async def test_an_unknown_runs_acts_are_a_422(monkeypatch):
    async with a_world(resting, monkeypatch).client() as client:
        unknown = await client.get(
            "/runs/run-nope/acts", headers={"Authorization": f"Bearer {OWNER_TOKEN}"}
        )
    assert unknown.status_code == 422
    assert isinstance(unknown, httpx.Response)
