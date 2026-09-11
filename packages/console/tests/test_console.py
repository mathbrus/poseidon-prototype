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

import asyncio
from pathlib import Path
from typing import Any

import httpx
import pytest
import yaml

from common.authn import Principal
from common.opa import NO_POLICY
from compiler import DeploymentPlan
from console import service as service_module
from console.app import create_app
from console.config import ConsoleSettings
from console.temporal import WorkflowNotFound
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRuns,
)

pytestmark = pytest.mark.anyio

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO_SAAS = (REPO_ROOT / "examples" / "hello-saas.yaml").read_text()
PLATFORM_NS = "poseidon-platform"
COMPANY = "hello-saas"
RUN_ID = "run-01CONSOLE"


@pytest.fixture
def anyio_backend() -> str:
    return "asyncio"


class FakeReviewer:
    async def review(self, token: str, audience: str) -> Principal | None:
        if audience != "poseidon-console":
            return None
        return {
            "t-owner": Principal.of(f"system:serviceaccount:{PLATFORM_NS}:human-owner"),
            "t-stranger": Principal.of(f"system:serviceaccount:{PLATFORM_NS}:human-x"),
            "t-agent": Principal.of(f"system:serviceaccount:{COMPANY}:pm-1"),
        }.get(token)


class FakeWorkflows:
    """Records what the console asked Temporal for, in order."""

    def __init__(self) -> None:
        self.calls: list[tuple[str, Any]] = []
        self.running: dict[str, str] = {}

    async def start(self, workflow: str, key: str, payload: dict[str, Any]) -> str:
        wf_id = f"{workflow}-{key}"
        self.calls.append(("start", (workflow, key, payload)))
        self.running[wf_id] = "running"
        return wf_id

    async def signal(self, wf_id: str, name: str, args: list[Any]) -> None:
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        self.calls.append(("signal", (wf_id, name, args)))

    async def cancel(self, wf_id: str) -> None:
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        self.calls.append(("cancel", wf_id))

    async def inspect(self, wf_id: str) -> dict[str, Any]:
        if wf_id not in self.running:
            raise WorkflowNotFound(wf_id)
        return {"workflow_id": wf_id, "status": "RUNNING", "state": self.running[wf_id]}


class World:
    def __init__(self) -> None:
        self.audit = InMemoryAuditStore()
        self.runs = InMemoryRuns()
        self.ledger = InMemoryBudgetLedger()
        self.instances = InMemoryAgentInstances()
        # The operator's record of the applied company; the console reads
        # which company it hosts from here.
        self.companies = InMemoryCompanies()
        self.companies.upsert(COMPANY, {"namespace": "poseidon-company"})
        self.workflows = FakeWorkflows()
        self.submitted: list[DeploymentPlan] = []
        self.deleted: list[str] = []
        # What OPA answers, per tool; the default is the rule the compiled
        # bundle would name for the owner.
        self.decisions: dict[str, dict[str, Any]] = {}
        self.asked: list[dict[str, Any]] = []

    def submit(self, plan: DeploymentPlan) -> tuple[int, int]:
        self.submitted.append(plan)
        return (1, 0)

    def delete(self, company: str) -> None:
        self.deleted.append(company)
        self.companies.delete(company)

    async def evaluate(self, opa_url: str, company: str, decision_input: dict) -> dict:
        self.asked.append(decision_input)
        tool = decision_input["tool"]
        if tool in self.decisions:
            return dict(self.decisions[tool])
        args = decision_input["args"]
        if tool == "gate.resolve":
            verdict = "approve" if args["approved"] else "reject"
            return {"allow": True, "rule": f"gate_resolve_{args['gate']}_{verdict}"}
        return {"allow": True, "rule": tool.replace(".", "_")}

    @property
    def events(self) -> list[tuple[str, str, str]]:
        return [(e.tool, e.decision, e.rule or "") for e in self.audit.events]


@pytest.fixture
def world(monkeypatch) -> World:
    w = World()
    monkeypatch.setattr(service_module, "evaluate_decision", w.evaluate)
    return w


@pytest.fixture
def client(world: World) -> httpx.AsyncClient:
    settings = ConsoleSettings(
        platform_namespace=PLATFORM_NS,
        opa_url="http://opa.test:8181",
        temporal_address="temporal.test:7233",
        console_humans="owner",
    )
    app = create_app(
        settings,
        audit=world.audit,
        runs=world.runs,
        ledger=world.ledger,
        instances=world.instances,
        companies=world.companies,
        token_reviewer=FakeReviewer(),
        workflows=world.workflows,
        submit_crs=world.submit,
        delete_company=world.delete,
        clock=lambda: "2026-01-01T00:00:00+00:00",
    )
    return httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app), base_url="http://console.test"
    )


def as_owner() -> dict[str, str]:
    return {"Authorization": "Bearer t-owner"}


def a_review_run(world: World) -> None:
    world.runs.start(RUN_ID, "Review PR #41", "review", f"review-{RUN_ID}", COMPANY)
    world.workflows.running[f"review-{RUN_ID}"] = "paused-merge_gate"


# -- the door ----------------------------------------------------------------


async def test_the_health_probe_is_the_only_open_route(client, world):
    assert (await client.get("/healthz")).status_code == 200
    assert (await client.get("/whoami")).status_code == 401
    assert world.audit.events == []


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
    assert response.json() == {"human": "owner", "lifecycle": "human", "company": COMPANY}


# -- a verdict: recorded, then delivered --------------------------------------


async def test_a_verdict_is_recorded_under_the_humans_name_then_delivered(client, world):
    a_review_run(world)
    response = await client.post(
        f"/runs/{RUN_ID}/gates/merge",
        json={"approved": True, "payload": "reviewed"},
        headers=as_owner(),
    )
    assert response.status_code == 200, response.text
    assert response.json()["principal"] == "owner"
    # The row first, under the rule the workflow reads it back by, then
    # the signal — and the registry's own read agrees.
    assert world.events == [("gate.resolve", "allow", "gate_resolve_merge_approve")]
    event = world.audit.events[0]
    assert (event.agent_id, event.lifecycle, event.run_id, event.workflow) == (
        "owner",
        "human",
        RUN_ID,
        f"review-{RUN_ID}",
    )
    assert world.workflows.calls == [
        ("signal", (f"review-{RUN_ID}", "merge", [True, "reviewed"]))
    ]
    assert world.audit.latest_human_verdict(RUN_ID, "merge") == {
        "principal": "owner",
        "approved": True,
        "ts": event.ts,
    }


async def test_a_denied_verdict_is_a_403_that_signals_nothing(client, world):
    a_review_run(world)
    world.decisions["gate.resolve"] = {"allow": False, "rule": "human_not_recognized"}
    response = await client.post(
        f"/runs/{RUN_ID}/gates/merge", json={"approved": True}, headers=as_owner()
    )
    assert response.status_code == 403
    assert response.json()["rule"] == "human_not_recognized"
    assert world.events == [("gate.resolve", "deny", "human_not_recognized")]
    assert world.workflows.calls == []
    assert world.audit.latest_human_verdict(RUN_ID, "merge") is None


async def test_a_rejection_is_recorded_as_one(client, world):
    a_review_run(world)
    world.workflows.running[f"review-{RUN_ID}"] = "paused-budget_exhausted"
    response = await client.post(
        f"/runs/{RUN_ID}/gates/budget", json={"approved": False}, headers=as_owner()
    )
    assert response.status_code == 200
    assert world.events == [("gate.resolve", "allow", "gate_resolve_budget_reject")]
    assert world.workflows.calls[-1] == (
        "signal",
        (f"review-{RUN_ID}", "budget", [False, None]),
    )


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
            f"/runs/{run_id}/gates/{gate}", json={"approved": True}, headers=as_owner()
        )
        assert response.status_code == 422, (run_id, gate, response.text)
    assert world.audit.events == []
    assert world.workflows.calls == []


# -- the other acts ----------------------------------------------------------


async def test_starting_a_run_is_decided_then_started(client, world):
    response = await client.post(
        "/runs", json={"workflow": "implement", "number": 42}, headers=as_owner()
    )
    assert response.status_code == 200, response.text
    run_id = response.json()["run_id"]
    assert response.json()["workflow_id"] == f"implement-{run_id}"
    assert world.asked[0]["tool"] == "run.start"
    assert world.asked[0]["args"] == {"workflow": "implement", "number": 42}
    assert world.asked[0]["lifecycle"] == "human"
    assert world.workflows.calls == [
        ("start", ("implement", run_id, {"run_id": run_id, "issue_number": 42}))
    ]
    assert world.events == [("run.start", "allow", "run_start")]


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
        f"/runs/{RUN_ID}/cancel", json={"reason": "enough"}, headers=as_owner()
    )
    woken = await client.post("/heartbeat/wake", headers=as_owner())
    assert cancelled.status_code == woken.status_code == 200
    assert world.workflows.calls == [
        ("cancel", f"review-{RUN_ID}"),
        ("signal", (f"heartbeat-{COMPANY}", "wake", [])),
    ]
    assert [e[0] for e in world.events] == ["run.cancel", "heartbeat.wake"]
    assert world.audit.events[1].run_id == f"heartbeat-{COMPANY}"


# -- the blueprint ---------------------------------------------------------


async def test_the_first_apply_is_the_bootstrap_rule(client, world):
    world.decisions["blueprint.apply"] = dict(NO_POLICY)
    response = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert response.status_code == 200, response.text
    assert response.json()["rule"] == "blueprint_apply_bootstrap"
    assert response.json()["created"] == 1
    assert [p.company.name for p in world.submitted] == [COMPANY]
    assert world.events == [("blueprint.apply", "allow", "blueprint_apply_bootstrap")]


async def test_a_later_apply_is_policys(client, world):
    response = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert response.status_code == 200
    assert response.json()["rule"] == "blueprint_apply"
    world.decisions["blueprint.apply"] = {"allow": False, "rule": "human_not_recognized"}
    denied = await client.post(
        "/blueprint/apply", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert denied.status_code == 403
    assert len(world.submitted) == 1


async def test_deleting_the_company_is_decided_audited_and_ends_its_runs_first(
    client, world
):
    """The one stop there is, as a console act: decided under the person's
    name, its running runs cancelled through the same path a cancel takes,
    then the Company CR deleted and the teardown waited for."""
    a_review_run(world)
    response = await client.request(
        "DELETE", "/company", json={"reason": "wrong vision"}, headers=as_owner()
    )
    assert response.status_code == 200, response.text
    assert response.json()["company"] == COMPANY
    assert response.json()["runs_cancelled"] == [RUN_ID]
    assert world.deleted == [COMPANY]
    assert world.workflows.calls == [("cancel", f"review-{RUN_ID}")]
    # The delete is recorded before anything is cancelled, and the cancel
    # under the same name says why.
    assert world.events == [
        ("company.delete", "allow", "company_delete"),
        ("run.cancel", "allow", "run_cancel"),
    ]
    assert world.audit.events[0].agent_id == "owner"
    # Nothing is left to act on.
    assert (await client.get("/heartbeat", headers=as_owner())).status_code == 404
    again = await client.request("DELETE", "/company", headers=as_owner())
    assert again.status_code == 404


async def test_a_stranger_cannot_delete_the_company(client, world):
    a_review_run(world)
    world.decisions["company.delete"] = {"allow": False, "rule": "human_not_recognized"}
    response = await client.request("DELETE", "/company", headers=as_owner())
    assert response.status_code == 403
    assert response.json()["rule"] == "human_not_recognized"
    assert world.deleted == [] and world.workflows.calls == []
    assert world.events == [("company.delete", "deny", "human_not_recognized")]


async def test_another_companys_blueprint_is_refused_while_one_is_applied(client, world):
    document = yaml.safe_load(HELLO_SAAS)
    document["company"]["name"] = "other-co"
    response = await client.post(
        "/blueprint/apply",
        json={"blueprint": yaml.safe_dump(document)},
        headers=as_owner(),
    )
    assert response.status_code == 422
    assert (
        "other-co" in response.json()["detail"] and COMPANY in response.json()["detail"]
    )
    assert world.submitted == [] and world.audit.events == []


async def test_before_the_first_apply_any_name_applies_and_nothing_else_answers(
    client, world
):
    """A fresh cluster hosts no company: the reads and acts that need one
    say so, `whoami` names none, and the first apply may bring any name."""
    world.companies.delete(COMPANY)
    who = await client.get("/whoami", headers=as_owner())
    assert who.json()["company"] is None
    for method, path in (("GET", "/heartbeat"), ("GET", "/spend"), ("GET", "/runs")):
        response = await client.request(method, path, headers=as_owner())
        assert response.status_code == 404, (path, response.text)
    wake = await client.post("/heartbeat/wake", headers=as_owner())
    assert wake.status_code == 404 and world.audit.events == []
    document = yaml.safe_load(HELLO_SAAS)
    document["company"]["name"] = "other-co"
    applied = await client.post(
        "/blueprint/apply",
        json={"blueprint": yaml.safe_dump(document)},
        headers=as_owner(),
    )
    assert applied.status_code == 200, applied.text
    assert applied.json()["company"] == "other-co"
    assert [e.company for e in world.audit.events] == ["other-co"]


async def test_preview_compiles_against_the_live_registry_and_writes_nothing(
    client, world
):
    fresh = await client.post(
        "/blueprint/preview", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert fresh.status_code == 200, fresh.text
    assert fresh.json()["plan"]["humans"] == ["owner"]
    assert "pm" in fresh.json()["diff"]
    # Once the registry knows the hire, the same document changes nothing.
    world.instances.upsert("pm-1", COMPANY, "pm", "pm", namespace=COMPANY)
    applied = await client.post(
        "/blueprint/preview", json={"blueprint": HELLO_SAAS}, headers=as_owner()
    )
    assert "no changes" in applied.json()["diff"]
    assert world.submitted == [] and world.audit.events == []


async def test_an_invalid_blueprint_says_what_is_wrong(client, world):
    response = await client.post(
        "/blueprint/preview", json={"blueprint": "company: {name: x}"}, headers=as_owner()
    )
    assert response.status_code == 422
    assert "required" in response.json()["detail"]


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

    run = (await client.get(f"/runs/{RUN_ID}", headers=as_owner())).json()
    assert run["execution"] == {
        "workflow_id": f"review-{RUN_ID}",
        "status": "RUNNING",
        "state": "paused-merge_gate",
    }
    runs = (await client.get("/runs", headers=as_owner())).json()
    assert [r["run_id"] for r in runs] == [RUN_ID]
    spend = (await client.get(f"/runs/{RUN_ID}/spend", headers=as_owner())).json()
    assert spend["usd"] == 0.25 and len(spend["entries"]) == 1
    total = (await client.get("/spend", headers=as_owner())).json()
    assert total == {"usd": 0.25, "prompt_tokens": 10, "completion_tokens": 5}
    now = (await client.get("/now", headers=as_owner())).json()
    assert now == {"now": "2026-01-01T00:00:00+00:00"}
    heartbeat = (await client.get("/heartbeat", headers=as_owner())).json()
    assert heartbeat["execution"]["status"] == "NOT_FOUND" and heartbeat["ticks"] == []
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

    world.audit.append(row(f"heartbeat-{COMPANY}", "heartbeat.tick"))
    world.audit.append(row(RUN_ID, "sandbox.exec"))
    world.audit.append(row("run-other", "git.open_pr"))
    world.ledger.record(
        company=COMPANY,
        run_id=RUN_ID,
        agent_id="implementer-1",
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

    page = (await client.get("/events", headers=as_owner())).json()
    assert [e["id"] for e in page["events"]] == [1, 2, 3]
    assert page["next"] == 3 and page["spend"] == {"usd": 0.25}
    later = (await client.get("/events?after=2", headers=as_owner())).json()
    assert [e["tool"] for e in later["events"]] == ["git.open_pr"] and later["next"] == 3
    nothing = (await client.get("/events?after=3", headers=as_owner())).json()
    assert nothing["events"] == [] and nothing["next"] == 3
    one_run = (await client.get(f"/events?run={RUN_ID}", headers=as_owner())).json()
    assert [e["id"] for e in one_run["events"]] == [2]
    assert one_run["spend"] == {"usd": 0.25, "run_usd": 0.25}
    assert world.audit.events[-1].tool == "git.open_pr"  # a read: nothing appended

    world.companies.delete(COMPANY)
    assert (await client.get("/events", headers=as_owner())).status_code == 404


async def test_a_story_is_the_runs_events_in_order(client, world):
    a_review_run(world)
    await client.post(
        f"/runs/{RUN_ID}/gates/merge", json={"approved": True}, headers=as_owner()
    )
    story = (await client.get(f"/runs/{RUN_ID}/story", headers=as_owner())).json()
    assert [(e["tool"], e["agent_id"], e["source"]) for e in story] == [
        ("gate.resolve", "owner", "console")
    ]


def test_the_route_table_matches_the_app():
    """The documentation renders ROUTES; every route the app serves is in
    it and nothing more."""
    from console.app import ROUTES

    settings = ConsoleSettings(
        platform_namespace=PLATFORM_NS,
        opa_url="http://opa.test:8181",
        temporal_address="temporal.test:7233",
    )
    app = create_app(
        settings,
        audit=InMemoryAuditStore(),
        runs=InMemoryRuns(),
        ledger=InMemoryBudgetLedger(),
        instances=InMemoryAgentInstances(),
        companies=InMemoryCompanies(),
        workflows=FakeWorkflows(),
        submit_crs=lambda plan: (0, 0),
        clock=lambda: "",
    )
    served = {
        (method, route.path)
        for route in app.routes
        if hasattr(route, "methods")
        for method in route.methods
        if method != "HEAD" and not route.path.startswith(("/docs", "/redoc", "/openapi"))
    }
    assert served == {(method, path) for method, path, _, _ in ROUTES}


def test_settings_split_the_declared_humans():
    settings = ConsoleSettings(
        platform_namespace=PLATFORM_NS,
        opa_url="http://opa.test:8181",
        temporal_address="temporal.test:7233",
        console_humans="owner, eval,",
    )
    assert settings.humans == ["owner", "eval"]
    asyncio.run(asyncio.sleep(0))
