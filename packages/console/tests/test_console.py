"""The console: verify, decide, do, audit — and nothing without all four.

What must hold:

- no token, 401 and no audit row (probes are not governance signals); a
  rejected token, 401 audited with the principal it carried;
- an act policy denies is a 403 that names the rule, audited, and does
  nothing — no signal, no CR;
- a verdict is recorded under the person's name *before* the signal, and
  under the rule the workflow reads it back by;
- the first apply of a company OPA serves no policy for is the bootstrap
  rule, audited as such; every later act is policy's;
- reads need a verified human and touch nothing.
"""

import copy
import hashlib

import httpx
import pytest
import yaml

from blueprint.bounds import render_bounds
from common.audit import HEARTBEAT_TICK, AuditEvent, gate_event
from common.console import (
    AgentList,
    ApplyResult,
    BlueprintView,
    BoundsView,
    CompanyDeleted,
    CompanyView,
    CrewView,
    DocumentView,
    EventsPage,
    ExecutionView,
    Health,
    HeartbeatView,
    LimitsView,
    Now,
    PauseState,
    PolicyRefusal,
    PreviewResult,
    Problem,
    RunCancelled,
    RunDetail,
    RunList,
    RunSpendView,
    RunStarted,
    SpendView,
    StoryView,
    TicketList,
    VerdictRecorded,
    WhoAmI,
    Woken,
)
from common.opa import NO_POLICY
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
from common.text import UNTRUSTED_CLIP, clip_head
from common.topology import BLUEPRINT_LIMIT_BYTES, PLATFORM_NAMESPACE
from common.workflows import GATE_PARKED, gate_rule, workflow_id
from console import fakes
from console import service as service_module
from console.config import ConsoleSettings
from console.fakes import COMPANY, World, applied_spec, as_owner, company_cr

pytestmark = pytest.mark.anyio

HELLO_SAAS = fakes.HELLO_SAAS.read_text()
PLATFORM_NS = PLATFORM_NAMESPACE
RUN_ID = "run-01CONSOLE"


def model(cls, response: httpx.Response):
    """The response, parsed as the interface model the route declares."""
    assert response.status_code == 200, response.text
    return cls.model_validate(response.json())


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    w.patch(monkeypatch.setattr)
    return w


@pytest.fixture
def client(world: World) -> httpx.AsyncClient:
    return world.client()


def a_review_run(world: World) -> None:
    world.runs.start(RUN_ID, "Review PR #41", "review", f"review-{RUN_ID}", COMPANY)
    world.workflows.running[f"review-{RUN_ID}"] = "parked-merge_gate"


def a_running_heartbeat(world: World) -> None:
    world.workflows.running[workflow_id("heartbeat", COMPANY)] = "sleeping"


def a_paused_company(world: World) -> None:
    world.companies.pause(COMPANY, "owner")


# -- the chokepoint ----------------------------------------------------------------


async def test_the_health_probe_is_the_only_open_route(client, world):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/whoami")).status_code == 401
    assert world.audit.events == []


async def test_the_health_probe_names_the_running_version(client, monkeypatch):
    """The images are stamped with the version at build time; the console
    is where a person reads which one the cluster runs."""
    monkeypatch.setenv("POSEIDON_VERSION", "v0.1.0+2026.10.21")
    assert model(Health, await client.get("/healthz")) == Health(
        ok=True, version="v0.1.0+2026.10.21"
    )
    monkeypatch.delenv("POSEIDON_VERSION")
    assert model(Health, await client.get("/healthz")).version == "unreleased"


async def test_a_rejected_token_is_audited_with_its_principal(client, world):
    stranger = await client.get("/whoami", headers={"Authorization": "Bearer t-stranger"})
    agent = await client.get("/whoami", headers={"Authorization": "Bearer t-agent"})
    assert stranger.status_code == agent.status_code == 401
    assert [(e.rule, e.agent_id, e.source) for e in world.audit.events] == [
        ("authn_not_a_human", f"system:serviceaccount:{PLATFORM_NS}:human-x", "console"),
        ("authn_not_a_human", f"system:serviceaccount:{COMPANY}:pm-1", "console"),
    ]


async def test_whoami_names_the_human_and_the_company(client):
    response = await client.get("/whoami", headers=as_owner())
    assert model(WhoAmI, response) == WhoAmI(
        human="owner", lifecycle="human", company=COMPANY
    )


# Each act as a client asks for it, in a world whose state lets it through:
# what the world needs, then the request.
ACTS = {
    BLUEPRINT_APPLY: (None, "POST", "/blueprint/apply", {"blueprint": HELLO_SAAS}),
    COMPANY_DELETE: (None, "DELETE", "/company", {"rationale": "mine now"}),
    RUN_START: (None, "POST", "/runs", {"workflow": "implement", "number": 7}),
    GATE_RESOLVE: (
        a_review_run,
        "POST",
        f"/runs/{RUN_ID}/gates/merge",
        {"approved": True, "rationale": "looks fine"},
    ),
    RUN_CANCEL: (a_review_run, "POST", f"/runs/{RUN_ID}/cancel", {"rationale": "mine"}),
    HEARTBEAT_WAKE: (a_running_heartbeat, "POST", "/heartbeat/wake", None),
    HEARTBEAT_PAUSE: (None, "POST", "/heartbeat/pause", {"rationale": "mine now"}),
    HEARTBEAT_RESUME: (
        a_paused_company,
        "POST",
        "/heartbeat/resume",
        {"rationale": "mine now"},
    ),
}


@pytest.mark.parametrize("act", ACTS)
async def test_an_act_policy_denies_is_a_403_that_does_nothing(act, client, world):
    """The refusal names policy's rule and is audited once; nothing else
    moves — no Temporal call, no Company CR submitted or deleted, no pause
    written or lifted. The same act, allowed, goes through: the world let
    it, policy refused it."""
    prepare, method, path, body = ACTS[act]
    if prepare is not None:
        prepare(world)
    cr, paused = copy.deepcopy(world.cr), world.companies.paused(COMPANY)
    world.decisions[act] = {"allow": False, "rule": "human_not_recognized"}
    denied = await client.request(method, path, json=body, headers=as_owner())
    assert denied.status_code == 403, denied.text
    assert PolicyRefusal.model_validate(denied.json()).rule == "human_not_recognized"
    assert world.events == [(act, "deny", "human_not_recognized")]
    assert world.calls == [] and world.submitted == []
    assert world.cr == cr and world.companies.paused(COMPANY) == paused
    assert COMPANY in world.companies.rows

    del world.decisions[act]
    allowed = await client.request(method, path, json=body, headers=as_owner())
    assert allowed.status_code == 200, allowed.text


# -- a verdict: recorded, then delivered --------------------------------------


async def test_a_verdict_is_recorded_under_the_humans_name_then_delivered(client, world):
    a_review_run(world)
    response = await client.post(
        f"/runs/{RUN_ID}/gates/merge",
        json={"approved": True, "rationale": "reviewed: findings are clean"},
        headers=as_owner(),
    )
    recorded = model(VerdictRecorded, response)
    assert (recorded.human, recorded.gate, recorded.approved) == ("owner", "merge", True)
    assert recorded.rule == "gate_resolve_merge_approve"
    # The row first, under the rule the workflow reads it back by and with
    # the person's reason as its rationale, then the signal — the verdict
    # alone, the row being the record — and the registry's read agrees.
    assert world.events == [(GATE_RESOLVE, "allow", "gate_resolve_merge_approve")]
    event = world.audit.events[0]
    assert (event.agent_id, event.lifecycle, event.run_id, event.workflow) == (
        "owner",
        "human",
        RUN_ID,
        f"review-{RUN_ID}",
    )
    assert event.rationale == "reviewed: findings are clean"
    assert world.workflows.calls == [("signal", (f"review-{RUN_ID}", "merge", [True]))]
    assert world.audit.latest_human_verdict(RUN_ID, "merge") == {
        "principal": "owner",
        "approved": True,
        "rationale": "reviewed: findings are clean",
        "ts": event.ts,
    }


async def test_a_rejection_is_recorded_as_one(client, world):
    a_review_run(world)
    world.workflows.running[f"review-{RUN_ID}"] = "parked-budget_exhausted"
    response = await client.post(
        f"/runs/{RUN_ID}/gates/budget",
        json={"approved": False, "rationale": "not worth more spend"},
        headers=as_owner(),
    )
    assert response.status_code == 200
    assert world.events == [(GATE_RESOLVE, "allow", "gate_resolve_budget_reject")]
    assert world.workflows.calls[-1] == (
        "signal",
        (f"review-{RUN_ID}", "budget", [False]),
    )
    assert world.audit.events[0].rationale == "not worth more spend"


async def test_an_act_without_a_rationale_is_refused_before_anything_happens(
    client, world
):
    """A person's rationale is part of the act: a verdict, a cancel, a
    delete, a pause and a resume without one are 422s that ask no policy,
    record nothing and signal nothing."""
    a_review_run(world)
    attempts = (
        ("POST", f"/runs/{RUN_ID}/gates/merge", {"approved": True}),
        ("POST", f"/runs/{RUN_ID}/gates/merge", {"approved": True, "rationale": ""}),
        ("POST", f"/runs/{RUN_ID}/cancel", None),
        ("POST", f"/runs/{RUN_ID}/cancel", {"rationale": ""}),
        ("DELETE", "/company", None),
        ("DELETE", "/company", {"rationale": "x" * 1001}),
        ("POST", "/heartbeat/pause", None),
        ("POST", "/heartbeat/pause", {"rationale": ""}),
        ("POST", "/heartbeat/resume", None),
    )
    for method, path, body in attempts:
        response = await client.request(method, path, json=body, headers=as_owner())
        assert response.status_code == 422, (method, path, body, response.text)
        detail = Problem.model_validate(response.json()).detail
        assert "rationale" in detail or detail == "body: Field required", detail
    assert world.audit.events == [] and world.workflows.calls == []


async def test_a_verdict_answers_only_a_question_that_was_asked(client, world):
    """An unknown run, a gate that does not exist, a gate the run's workflow
    does not have, and a gate the run is not parked on: refused before
    policy is asked, nothing recorded, nothing signalled."""
    a_review_run(world)
    world.runs.start(
        "run-impl", "Implement #7", "implement", "implement-run-impl", COMPANY
    )
    world.workflows.running["implement-run-impl"] = "running"
    refusals = [
        ("run-nope", "merge"),
        (RUN_ID, "door"),
        ("run-impl", "merge"),
        (RUN_ID, "budget"),
    ]
    for run_id, gate in refusals:
        response = await client.post(
            f"/runs/{run_id}/gates/{gate}",
            json={"approved": True, "rationale": "yes"},
            headers=as_owner(),
        )
        assert response.status_code == 422, (run_id, gate, response.text)
        Problem.model_validate(response.json())
    assert world.audit.events == []
    assert world.workflows.calls == []


# -- the other acts ----------------------------------------------------------


async def test_starting_a_run_is_decided_then_started(client, world):
    response = await client.post(
        "/runs", json={"workflow": "implement", "number": 42}, headers=as_owner()
    )
    started = model(RunStarted, response)
    run_id = started.run_id
    assert started.workflow_id == f"implement-{run_id}"
    assert started.rule == "run_start"
    assert world.asked[0]["tool"] == RUN_START
    assert world.asked[0]["args"] == {"workflow": "implement", "number": 42}
    assert world.asked[0]["lifecycle"] == "human"
    assert world.workflows.calls == [
        ("start", ("implement", run_id, {"run_id": run_id, "issue_number": 42}))
    ]
    assert world.events == [(RUN_START, "allow", "run_start")]


async def test_a_second_start_of_a_held_item_is_refused_before_policy(client, world):
    """One live run per work item: starting a ticket a live implement run
    holds (or a pull request a live review holds) is a 422 that asks no
    policy and starts nothing; once that run ends, the item may start
    again."""
    world.runs.start("run-live", "implement issue #42", "implement", None, COMPANY)
    world.runs.start("run-rev", "review pull request #7", "review", None, COMPANY)
    for workflow, number, item in (
        ("implement", 42, "ticket #42"),
        ("review", 7, "pull request #7"),
    ):
        response = await client.post(
            "/runs", json={"workflow": workflow, "number": number}, headers=as_owner()
        )
        assert response.status_code == 422, response.text
        assert item in Problem.model_validate(response.json()).detail
    assert world.asked == [] and world.workflows.calls == []
    world.runs.finish("run-live", "failed", error="aborted")
    again = await client.post(
        "/runs", json={"workflow": "implement", "number": 42}, headers=as_owner()
    )
    assert model(RunStarted, again).workflow == "implement"


async def test_only_the_human_startable_workflows_can_be_started(client, world):
    response = await client.post(
        "/runs", json={"workflow": "scope", "number": 1}, headers=as_owner()
    )
    assert response.status_code == 422
    assert world.workflows.calls == []


async def test_cancel_and_wake_are_decided_acts(client, world):
    a_review_run(world)
    world.workflows.running[f"heartbeat-{COMPANY}"] = "sleeping"
    cancelled = await client.post(
        f"/runs/{RUN_ID}/cancel", json={"rationale": "enough"}, headers=as_owner()
    )
    woken = await client.post("/heartbeat/wake", headers=as_owner())
    assert model(RunCancelled, cancelled).workflow_id == f"review-{RUN_ID}"
    assert model(Woken, woken) == Woken(
        company=COMPANY, workflow_id=f"heartbeat-{COMPANY}", rule="heartbeat_wake"
    )
    assert world.workflows.calls == [
        ("cancel", f"review-{RUN_ID}"),
        ("signal", (f"heartbeat-{COMPANY}", "wake", [])),
    ]
    assert [e[0] for e in world.events] == [RUN_CANCEL, HEARTBEAT_WAKE]
    assert world.audit.events[0].rationale == "enough"
    assert world.audit.events[1].run_id == f"heartbeat-{COMPANY}"


async def test_pause_is_recorded_before_the_loop_hears_and_resume_mirrors_it(
    client, world
):
    """The verdict's order, for the loop: policy, the audit row under the
    heartbeat's story with the reason, the registry fact every tick reads,
    then one wake. `GET /company` shows who holds it; resume clears it."""
    world.workflows.running[f"heartbeat-{COMPANY}"] = "sleeping"
    world.cr = company_cr(COMPANY, phase="converged")
    paused = await client.post(
        "/heartbeat/pause", json={"rationale": "reading the ledger"}, headers=as_owner()
    )
    assert model(PauseState, paused).paused.by == "owner"
    assert world.companies.paused(COMPANY)["by"] == "owner"
    assert world.workflows.calls == [("signal", (f"heartbeat-{COMPANY}", "wake", []))]
    assert world.events == [(HEARTBEAT_PAUSE, "allow", "heartbeat_pause")]
    (row,) = world.audit.events
    assert row.rationale == "reading the ledger"
    assert row.run_id == f"heartbeat-{COMPANY}" and row.agent_id == "owner"
    shown = await client.get("/company", headers=as_owner())
    assert model(CompanyView, shown).paused.by == "owner"

    resumed = await client.post(
        "/heartbeat/resume", json={"rationale": "read it"}, headers=as_owner()
    )
    assert model(PauseState, resumed).paused is None
    assert world.companies.paused(COMPANY) is None
    assert world.workflows.calls[-1] == ("signal", (f"heartbeat-{COMPANY}", "wake", []))
    assert world.events[-1] == (HEARTBEAT_RESUME, "allow", "heartbeat_resume")
    assert world.audit.events[-1].rationale == "read it"
    assert (
        model(CompanyView, await client.get("/company", headers=as_owner())).paused
        is None
    )


async def test_a_pause_nobody_asked_for_is_refused_before_policy(client, world):
    """Pausing a paused company, or resuming one that is not, is a 422:
    no policy asked, no row, no signal — the verdict-on-no-gate idiom."""
    world.workflows.running[f"heartbeat-{COMPANY}"] = "sleeping"
    resumed = await client.post(
        "/heartbeat/resume", json={"rationale": "go"}, headers=as_owner()
    )
    assert resumed.status_code == 422
    await client.post("/heartbeat/pause", json={"rationale": "hold"}, headers=as_owner())
    again = await client.post(
        "/heartbeat/pause", json={"rationale": "hold again"}, headers=as_owner()
    )
    assert again.status_code == 422
    assert [e[0] for e in world.events] == [HEARTBEAT_PAUSE]
    assert len(world.workflows.calls) == 1


async def test_a_pause_holds_a_loop_the_worker_has_not_started_yet(client, world):
    """The fact is the record: with no heartbeat workflow to wake, the
    pause is still written, and the loop reads it when the worker starts
    it."""
    paused = await client.post(
        "/heartbeat/pause",
        json={"rationale": "before the first tick"},
        headers=as_owner(),
    )
    assert model(PauseState, paused).paused.by == "owner"
    assert world.companies.paused(COMPANY)["by"] == "owner"
    assert world.workflows.calls == []


# -- the blueprint ---------------------------------------------------------


async def test_the_first_apply_is_the_bootstrap_rule(client, world):
    world.decisions[BLUEPRINT_APPLY] = dict(NO_POLICY)
    response = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    applied = model(ApplyResult, response)
    assert applied.rule == "blueprint_apply_bootstrap"
    assert applied.created == 1
    assert [p.company.name for p in world.submitted] == [COMPANY]
    assert world.events == [(BLUEPRINT_APPLY, "allow", "blueprint_apply_bootstrap")]


async def test_a_later_apply_is_policys(client, world):
    response = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert model(ApplyResult, response).rule == "blueprint_apply"
    world.decisions[BLUEPRINT_APPLY] = {"allow": False, "rule": "human_not_recognized"}
    denied = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert denied.status_code == 403
    assert len(world.submitted) == 1


async def test_deleting_the_company_is_decided_audited_and_ends_its_runs_first(
    client, world
):
    """The one stop there is, as a console act, in its order: decided under
    the person's name; the heartbeat held and woken until it says so; the
    running runs cancelled through the path a cancel takes and waited for
    until closed; then the Company CR deleted and its teardown waited for;
    then every workflow history deleted, the heartbeat's latest run named
    in case the listing lags it."""
    heartbeat = f"heartbeat-{COMPANY}"
    world.workflows.running[heartbeat] = "sleeping"
    a_review_run(world)

    response = await client.request(
        "DELETE", "/company", json={"rationale": "wrong vision"}, headers=as_owner()
    )
    deleted = model(CompanyDeleted, response)
    assert deleted.company == COMPANY
    assert deleted.runs_cancelled == [RUN_ID]
    assert (deleted.rule, deleted.purged_remote) == ("company_delete", False)
    assert world.calls == [
        ("signal", (heartbeat, "wake", [])),
        ("cancel", f"review-{RUN_ID}"),
        ("delete_company", COMPANY),
        ("delete_executions", [heartbeat]),
    ]
    # The act recorded first, the cancel after it under the same name, and
    # the loop held under it.
    assert [
        (e.tool, e.decision, e.agent_id, e.rationale) for e in world.audit.events
    ] == [
        (COMPANY_DELETE, "allow", "owner", "wrong vision"),
        (RUN_CANCEL, "allow", "owner", "company deleted: wrong vision"),
    ]
    assert world.companies.pauses[COMPANY]["by"] == "owner"
    # Nothing is left to act on.
    assert (await client.get("/heartbeat", headers=as_owner())).status_code == 404
    again = await client.request(
        "DELETE", "/company", json={"rationale": "again"}, headers=as_owner()
    )
    assert again.status_code == 404


async def test_a_delete_may_purge_the_repository_once_the_runs_have_ended(client, world):
    """`purge_remote`: decided under its own rule, then the repository
    reset through the gateway after the runs closed and before the Company
    CR goes — while the company's policy still allows the console that one
    call."""
    a_review_run(world)
    response = await client.request(
        "DELETE",
        "/company",
        json={"rationale": "start over", "purge_remote": True},
        headers=as_owner(),
    )
    deleted = model(CompanyDeleted, response)
    assert (deleted.rule, deleted.purged_remote) == ("company_delete_purge_remote", True)
    repo = applied_spec()["repo"]
    assert [call[0] for call in world.calls] == [
        "cancel",
        "purge",
        "delete_company",
        "delete_executions",
    ]
    assert ("purge", (repo["url"], repo["base_ref"])) in world.calls


async def test_a_refused_purge_fails_the_delete_before_the_company_goes(client, world):
    """A git host that refuses the reset — a protected base branch — fails
    the act: the company stays applied and held, its runs ended, and a
    second delete finishes it once the branch allows it."""
    a_review_run(world)
    world.gateway.purge_refusal = "Cannot force-push to this branch"
    response = await client.request(
        "DELETE",
        "/company",
        json={"rationale": "start over", "purge_remote": True},
        headers=as_owner(),
    )
    assert response.status_code == 503
    assert "Cannot force-push" in Problem.model_validate(response.json()).detail
    assert world.calls == [("cancel", f"review-{RUN_ID}")]
    assert world.companies.paused(COMPANY)["by"] == "owner"

    world.gateway.purge_refusal = None
    again = await client.request(
        "DELETE",
        "/company",
        json={"rationale": "again", "purge_remote": True},
        headers=as_owner(),
    )
    assert model(CompanyDeleted, again).purged_remote is True
    assert ("delete_company", COMPANY) in world.calls


async def test_a_delete_that_cannot_finish_leaves_the_company_held_and_a_second_finishes(
    client, world, monkeypatch
):
    """A heartbeat that does not hold in time — a tick still deciding —
    fails the act before anything is torn down: the company stays applied
    and held, and asking again finishes the delete."""
    monkeypatch.setattr(service_module, "TEARDOWN_TIMEOUT_S", 0)
    heartbeat = f"heartbeat-{COMPANY}"
    world.workflows.running[heartbeat] = "ticking"
    world.workflows.on_signal = lambda wf_id, name: None  # still mid-tick

    response = await client.request(
        "DELETE", "/company", json={"rationale": "first"}, headers=as_owner()
    )
    assert response.status_code == 503
    assert "the heartbeat has not held" in Problem.model_validate(response.json()).detail
    assert world.calls == [("signal", (heartbeat, "wake", []))]
    assert world.companies.paused(COMPANY)["by"] == "owner"

    world.workflows.running[heartbeat] = "paused"  # the tick ended, held
    deleted = model(
        CompanyDeleted,
        await client.request(
            "DELETE", "/company", json={"rationale": "again"}, headers=as_owner()
        ),
    )
    assert deleted.company == COMPANY and ("delete_company", COMPANY) in world.calls
    assert world.companies.applied() is None


async def test_the_company_read_is_the_crs_phase(client, world):
    """What `apply` waits on: the CR's phase and the generation it answers
    for — read from the cluster, since the registry row exists only once
    the company is converged."""
    assert (await client.get("/company", headers=as_owner())).status_code == 404
    applied = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert model(ApplyResult, applied).generation == 1
    state = model(CompanyView, await client.get("/company", headers=as_owner()))
    assert state.company == COMPANY and state.phase == "activating"
    assert state.generation == 1 and state.observed_generation == 0
    world.cr["status"].update(
        phase="converged", observedGeneration=1, bundleRevision="r1"
    )
    state = model(CompanyView, await client.get("/company", headers=as_owner()))
    assert state.phase == "converged" and state.bundle_revision == "r1"


async def test_the_applied_blueprint_reads_back_byte_for_byte(client, world):
    """The CR keeps the text a human applied — comments, ordering and all
    — with who applied it and when; the generation and phase say whether
    policy enforces it yet."""
    assert (await client.get("/blueprint", headers=as_owner())).status_code == 404
    text = "# the owner's own words\n" + HELLO_SAAS
    applied = await client.post(
        "/blueprint/apply", json={"blueprint": text}, headers=as_owner()
    )
    assert model(ApplyResult, applied).generation == 1
    shown = model(BlueprintView, await client.get("/blueprint", headers=as_owner()))
    assert shown.text == text
    assert shown.sha256 == hashlib.sha256(text.encode()).hexdigest()
    assert (shown.applied_by, shown.applied_at.isoformat()) == ("owner", world.now)
    assert (shown.generation, shown.observed_generation, shown.phase) == (
        1,
        0,
        "activating",
    )
    world.cr["status"].update(phase="converged", observedGeneration=1)
    shown = model(BlueprintView, await client.get("/blueprint", headers=as_owner()))
    assert shown.phase == "converged"


async def test_a_blueprint_over_the_limit_is_refused(client, world):
    """The CR keeps the text, so the text is bounded: a longer one is a 422
    before anything is compiled, decided or written — counted in bytes,
    so text in any script is held to the same limit."""
    padding = "#" + "x" * BLUEPRINT_LIMIT_BYTES + "\n"
    long = await client.post(
        "/blueprint/apply", json={"blueprint": padding + HELLO_SAAS}, headers=as_owner()
    )
    assert long.status_code == 422, long.text
    wide = "# " + "é" * (BLUEPRINT_LIMIT_BYTES // 2) + "\n" + HELLO_SAAS
    assert len(wide) <= BLUEPRINT_LIMIT_BYTES < len(wide.encode())
    refused = await client.post(
        "/blueprint/apply", json={"blueprint": wide}, headers=as_owner()
    )
    assert refused.status_code == 422, refused.text
    assert "bytes" in Problem.model_validate(refused.json()).detail
    assert world.submitted == [] and world.audit.events == []


async def test_another_companys_blueprint_is_refused_while_one_is_applied(client, world):
    document = yaml.safe_load(HELLO_SAAS)
    document["company"]["name"] = "other-co"
    response = await client.post(
        "/blueprint/apply",
        json={"blueprint": yaml.safe_dump(document)},
        headers=as_owner(),
    )
    assert response.status_code == 422
    detail = Problem.model_validate(response.json()).detail
    assert "other-co" in detail and COMPANY in detail
    assert world.submitted == [] and world.audit.events == []


async def test_the_repository_cannot_change_while_the_company_lives(client, world):
    """Preview and apply both refuse another `stack.repo.url`, naming both
    repositories and the way out; the same repository with other changes
    still applies."""
    document = yaml.safe_load(HELLO_SAAS)
    document["stack"]["repo"]["url"] = "github.com/example-org/elsewhere"
    for path in ("/blueprint/preview", "/blueprint/apply"):
        response = await client.post(
            path, json={"blueprint": yaml.safe_dump(document)}, headers=as_owner()
        )
        assert response.status_code == 422, path
        detail = Problem.model_validate(response.json()).detail
        assert "github.com/example-org/hello-saas" in detail
        assert "github.com/example-org/elsewhere" in detail
        assert "delete the company" in detail
    assert world.submitted == [] and world.audit.events == []
    same = yaml.safe_load(HELLO_SAAS)
    same["budgets"]["per_run"]["usd"] = 7
    applied = await client.post(
        "/blueprint/apply", json={"blueprint": yaml.safe_dump(same)}, headers=as_owner()
    )
    assert model(ApplyResult, applied).company == COMPANY


async def test_a_blueprint_naming_a_stranger_is_refused(client, world):
    """The console verifies humans, it does not make them: where the chart
    declares who a person can be, a blueprint may only name those people."""
    document = yaml.safe_load(HELLO_SAAS)
    document["company"]["humans"] = ["owner", "stranger"]
    response = await client.post(
        "/blueprint/preview",
        json={"blueprint": yaml.safe_dump(document)},
        headers=as_owner(),
    )
    assert response.status_code == 422
    assert "'stranger'" in response.json()["detail"]
    assert "declared: owner" in response.json()["detail"]
    assert world.submitted == [] and world.audit.events == []


async def test_before_the_first_apply_any_name_applies_and_nothing_else_answers(
    client, world
):
    """A fresh cluster hosts no company: the reads and acts that need one
    say so, `whoami` names none, and the first apply may bring any name."""
    del world.companies.rows[COMPANY]
    who = await client.get("/whoami", headers=as_owner())
    assert model(WhoAmI, who).company is None
    for method, path in (("GET", "/heartbeat"), ("GET", "/spend"), ("GET", "/runs")):
        response = await client.request(method, path, headers=as_owner())
        assert response.status_code == 404, (path, response.text)
        Problem.model_validate(response.json())
    wake = await client.post("/heartbeat/wake", headers=as_owner())
    assert wake.status_code == 404 and world.audit.events == []
    document = yaml.safe_load(HELLO_SAAS)
    document["company"]["name"] = "other-co"
    applied = await client.post(
        "/blueprint/apply",
        json={"blueprint": yaml.safe_dump(document)},
        headers=as_owner(),
    )
    assert model(ApplyResult, applied).company == "other-co"
    assert [e.company for e in world.audit.events] == ["other-co"]


async def test_preview_compiles_against_the_live_registry_and_writes_nothing(
    client, world
):
    fresh = await client.post(
        "/blueprint/preview", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert model(PreviewResult, fresh).company == COMPANY
    assert "pm" in model(PreviewResult, fresh).diff
    # Once the registry knows the hire, the same document changes nothing.
    world.instances.upsert("pm-1", COMPANY, "pm", "pm", namespace=COMPANY)
    applied = await client.post(
        "/blueprint/preview", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert "no changes" in model(PreviewResult, applied).diff
    assert world.submitted == [] and world.audit.events == []


async def test_an_invalid_blueprint_says_what_is_wrong(client, world):
    response = await client.post(
        "/blueprint/preview", json={"blueprint": "company: {name: x}"}, headers=as_owner()
    )
    assert response.status_code == 422
    assert "required" in Problem.model_validate(response.json()).detail


# -- the reads ---------------------------------------------------------------


async def test_the_reads_need_a_human_and_touch_nothing(client, world):
    a_review_run(world)
    world.ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id="reviewer",
        parent_agent_id="pm-1",
        role="reviewer",
        model="mock",
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=0,
        reasoning_tokens=0,
        usd=0.25,
    )
    for path in (f"/runs/{RUN_ID}", "/runs", "/spend", "/heartbeat", "/now"):
        assert (await client.get(path)).status_code == 401, path

    run = model(RunDetail, await client.get(f"/runs/{RUN_ID}", headers=as_owner()))
    assert run.execution == ExecutionView(
        workflow_id=f"review-{RUN_ID}", status="RUNNING", state="parked-merge_gate"
    )
    assert (run.workflow, run.status, run.task) == ("review", "running", "Review PR #41")
    runs = model(RunList, await client.get("/runs", headers=as_owner()))
    assert [r.run_id for r in runs.runs] == [RUN_ID]
    spend = model(
        RunSpendView, await client.get(f"/runs/{RUN_ID}/spend", headers=as_owner())
    )
    assert spend.usd == 0.25 and [e.model for e in spend.entries] == ["mock"]
    total = model(SpendView, await client.get("/spend", headers=as_owner()))
    assert total == SpendView(usd=0.25, prompt_tokens=10, completion_tokens=5)
    now = model(Now, await client.get("/now", headers=as_owner()))
    assert now.now.isoformat() == "2026-01-01T00:00:00+00:00"
    heartbeat = model(HeartbeatView, await client.get("/heartbeat", headers=as_owner()))
    assert heartbeat.execution.status == "NOT_FOUND" and heartbeat.ticks == []
    assert world.workflows.calls == [] and world.audit.events == []


async def test_events_are_the_companys_rows_after_a_cursor_with_spend(client, world):
    """The follower's read: rows above a cursor in id order, the cursor to
    continue from, the company's spend to date — and one run's when the
    read is narrowed. Without a company applied there is nothing to
    follow; without a token, nothing at all."""
    from common.audit import AuditEvent

    def row(run_id: str, tool: str) -> AuditEvent:
        return AuditEvent(
            run_id=run_id,
            agent_id="pm-1",
            role="pm",
            tool=tool,
            args_digest="d",
            decision="allow",
            rule=None,
            latency_ms=1,
            backend_status="ok",
            source="gateway",
            company=COMPANY,
        )

    world.audit.append(row(f"heartbeat-{COMPANY}", HEARTBEAT_TICK))
    world.audit.append(row(RUN_ID, "sandbox.exec"))
    world.audit.append(row("run-other", "git.open_pr"))
    world.ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id=f"feature-delivery-implementer-{RUN_ID}",
        parent_agent_id="pm-1",
        role="implementer",
        model="mock",
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=0,
        reasoning_tokens=0,
        usd=0.25,
    )
    assert (await client.get("/events")).status_code == 401

    async def page(query: str = "") -> EventsPage:
        return model(EventsPage, await client.get(f"/events{query}", headers=as_owner()))

    first = await page()
    assert [e.id for e in first.events] == [1, 2, 3]
    assert first.next == 3 and (first.spend.usd, first.spend.run_usd) == (0.25, None)
    later = await page("?after=2")
    assert [e.tool for e in later.events] == ["git.open_pr"] and later.next == 3
    nothing = await page("?after=3")
    assert nothing.events == [] and nothing.next == 3
    one_run = await page(f"?run={RUN_ID}")
    assert [e.id for e in one_run.events] == [2]
    assert (one_run.spend.usd, one_run.spend.run_usd) == (0.25, 0.25)
    assert world.audit.events[-1].tool == "git.open_pr"  # a read: nothing appended

    del world.companies.rows[COMPANY]
    assert (await client.get("/events", headers=as_owner())).status_code == 404


async def test_a_decisions_story_is_the_rows_with_a_rationale(client, world):
    """`?decisions=true` reads the choices and their reasons alone: the
    verdict a person recorded, not the tool calls around it."""
    a_review_run(world)
    world.audit.append(
        AuditEvent(
            ts="2026-01-01T00:00:00+00:00",
            run_id=RUN_ID,
            agent_id="reviewer-1",
            role="reviewer",
            tool="git.read",
            args_digest="d",
            decision="allow",
            rule="crew_feature_review_reviewer_git.read",
            latency_ms=1,
            backend_status="ok",
            source="gateway",
            company=COMPANY,
        )
    )
    await client.post(
        f"/runs/{RUN_ID}/gates/merge",
        json={"approved": True, "rationale": "clean findings"},
        headers=as_owner(),
    )
    everything = model(
        StoryView, await client.get(f"/runs/{RUN_ID}/story", headers=as_owner())
    )
    decisions = model(
        StoryView,
        await client.get(f"/runs/{RUN_ID}/story?decisions=true", headers=as_owner()),
    )
    assert [e.tool for e in everything.events] == ["git.read", GATE_RESOLVE]
    assert [(e.tool, e.rationale) for e in decisions.events] == [
        (GATE_RESOLVE, "clean findings")
    ]


# -- what a web client reads ---------------------------------------------------


def park(world: World, run_id: str, gate: str, ts: str) -> None:
    """The `parked` gate row a workflow writes before it reads parked."""
    world.audit.append(
        AuditEvent(
            ts=ts,
            run_id=run_id,
            agent_id="orchestrator",
            role="platform",
            tool=gate_event(gate),
            args_digest="d",
            decision="deny",
            rule=gate_rule(GATE_PARKED),
            latency_ms=1,
            backend_status=None,
            source="process-gate",
            company=COMPANY,
        )
    )


async def test_parked_runs_carry_their_gate_and_deadlines(client, world):
    """`?state=parked` keeps the runs parked on a gate, each with the
    deadline the applied spec gives it, measured from its `parked` row:
    escalation at half, expiry at the end."""
    a_review_run(world)
    park(world, RUN_ID, "merge", "2026-01-01T00:00:00+00:00")
    world.runs.start("run-impl", "implement issue #7", "implement", None, COMPANY)
    world.workflows.running["implement-run-impl"] = "running"

    every = model(RunList, await client.get("/runs", headers=as_owner()))
    assert [(r.run_id, r.parked is not None) for r in every.runs] == [
        (RUN_ID, True),
        ("run-impl", False),
    ]
    parked = model(RunList, await client.get("/runs?state=parked", headers=as_owner()))
    (run,) = parked.runs
    assert run.run_id == RUN_ID and run.parked.gate == "merge"
    assert run.parked.deadline_minutes == 1440
    assert run.parked.since.isoformat() == "2026-01-01T00:00:00+00:00"
    assert run.parked.escalates_at.isoformat() == "2026-01-01T12:00:00+00:00"
    assert run.parked.expires_at.isoformat() == "2026-01-02T00:00:00+00:00"
    detail = model(RunDetail, await client.get(f"/runs/{RUN_ID}", headers=as_owner()))
    assert detail.parked == run.parked
    assert world.audit.events[-1].rule == gate_rule(GATE_PARKED)  # a read wrote nothing


async def test_a_budget_park_is_measured_by_the_budget_deadline(client, world):
    a_review_run(world)
    world.workflows.running[f"review-{RUN_ID}"] = "parked-budget_exhausted"
    park(world, RUN_ID, "budget", "2026-01-01T00:00:00+00:00")
    run = model(RunDetail, await client.get(f"/runs/{RUN_ID}", headers=as_owner()))
    assert (run.parked.gate, run.parked.deadline_minutes) == ("budget", 60)
    assert run.parked.expires_at.isoformat() == "2026-01-01T01:00:00+00:00"


async def test_a_runs_crew_is_every_identity_ever_bound(client, world):
    a_review_run(world)
    world.participants.add(
        "reviewer-1", RUN_ID, "pm-1", "feature-review", "reviewer", COMPANY
    )
    world.participants.add("other-1", "run-other", "pm-1", "feature-review", "reviewer")
    world.participants.terminate("reviewer-1")
    crew = model(CrewView, await client.get(f"/runs/{RUN_ID}/crew", headers=as_owner()))
    assert [(a.agent_id, a.crew, a.role, a.status) for a in crew.agents] == [
        ("reviewer-1", "feature-review", "reviewer", "terminated")
    ]
    unknown = await client.get("/runs/run-nope/crew", headers=as_owner())
    assert unknown.status_code == 422


async def test_the_standing_agents_name_the_conductor(client, world):
    world.instances.upsert("pm-1", COMPANY, "pm", "pm", namespace="poseidon-company")
    world.instances.upsert("qa-1", COMPANY, "qa", "reviewer", status="pending")
    agents = model(AgentList, await client.get("/agents", headers=as_owner()))
    assert [(a.standing_name, a.conductor, a.status) for a in agents.agents] == [
        ("pm", True, "converged"),
        ("qa", False, "pending"),
    ]


async def test_the_tickets_are_the_ones_filed_on_the_company_repository(client, world):
    repo = applied_spec()["repo"]["url"]
    world.tickets.record(repo, 9, "run-scope")
    world.tickets.record(repo, 3, "heartbeat-hello-saas")
    world.tickets.record("github.com/elsewhere/repo", 1, "run-x")
    tickets = model(TicketList, await client.get("/tickets", headers=as_owner()))
    assert tickets.repo == repo
    assert [(t.number, t.run_id) for t in tickets.tickets] == [
        (3, "heartbeat-hello-saas"),
        (9, "run-scope"),
    ]


async def test_the_documents_are_read_live_at_the_base_ref(client, world):
    """Each of the two documents, read through the gateway at the company's
    repository and base ref, clipped as a crew reads it; an absent one is
    `present: false`, not an error."""
    spec = applied_spec()
    world.gateway.files[spec["documents"]["strategy"]] = "x" * (UNTRUSTED_CLIP + 10)
    strategy = model(
        DocumentView, await client.get("/documents/strategy", headers=as_owner())
    )
    assert (strategy.kind, strategy.path, strategy.ref, strategy.present) == (
        "strategy",
        spec["documents"]["strategy"],
        spec["repo"]["base_ref"],
        True,
    )
    assert strategy.text == clip_head("x" * (UNTRUSTED_CLIP + 10))
    architecture = model(
        DocumentView, await client.get("/documents/architecture", headers=as_owner())
    )
    assert architecture.present is False and architecture.text is None
    assert world.gateway.reads == [
        (spec["repo"]["url"], spec["repo"]["base_ref"], spec["documents"][name])
        for name in ("strategy", "architecture")
    ]
    unknown = await client.get("/documents/roadmap", headers=as_owner())
    assert unknown.status_code == 422 and world.audit.events == []


async def test_the_company_carries_its_limits_and_its_bounds(client, world):
    world.cr = company_cr(COMPANY, phase="converged", observedGeneration=1)
    world.ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id="reviewer-1",
        parent_agent_id="pm-1",
        role="reviewer",
        model="mock",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        reasoning_tokens=0,
        usd=1.5,
    )
    limits = model(CompanyView, await client.get("/company", headers=as_owner())).limits
    assert limits == LimitsView(
        per_run_usd=5.0,
        daily_cap_usd=20.0,
        spent_today_usd=1.5,
        max_concurrent_runs=1,
        gate_deadline_minutes={"budget": 60, "merge": 1440},
    )
    bounds = model(BoundsView, await client.get("/company/bounds", headers=as_owner()))
    assert bounds.text == render_bounds(applied_spec())
    assert bounds.text.endswith("Merge: every pull request waits for a human's verdict.")


async def test_before_it_converges_a_company_has_no_limits_and_no_bounds(client, world):
    """The CR is there, the reconciler's copy of the spec is not yet: the
    company reads activating without limits, and what needs the applied
    spec says so."""
    del world.companies.rows[COMPANY]
    await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    shown = model(CompanyView, await client.get("/company", headers=as_owner()))
    assert shown.phase == "activating" and shown.limits is None
    for path in ("/company/bounds", "/agents", "/tickets"):
        response = await client.get(path, headers=as_owner())
        assert response.status_code == 404, (path, response.text)


def test_settings_split_the_declared_humans():
    settings = ConsoleSettings(
        platform_namespace=PLATFORM_NS,
        opa_url="http://opa.test:8181",
        temporal_address="temporal.test:7233",
        console_humans="owner, eval,",
    )
    assert settings.humans == ["owner", "eval"]
