"""The run lifecycle activities against doubles: the run row, the
budget-headroom boundary, the workflow shape read fail-closed from the
applied blueprint, gate outcomes as first-class process-gate audit events,
teardown on every terminal path, and the attempt bound a run reports after
it. Sequencing, compensation and durability are workflow concerns, proven
live by the kind e2e suite (worker-kill included)."""

import asyncio
from dataclasses import asdict
from typing import Any

import pytest

from blueprint.bounds import render_bounds
from common.audit import LIFECYCLE_REVOKE, gate_event
from common.topology import COMPANY_NAMESPACE
from common.workflows import ATTEMPTS_GATE, MERGE_GATE
from orchestrator.activities.crew import CrewActivities
from orchestrator.activities.run import RunActivities, attempts_exhausted_comment
from orchestrator.activities.tickets import refused_comment
from orchestrator.workflows.base import Timeouts
from registry.memory import (
    InMemoryAgentInstances,
    InMemoryAuditStore,
    InMemoryBudgetLedger,
    InMemoryCompanies,
    InMemoryRunParticipants,
    InMemoryRuns,
)
from sandbox.contract import SandboxHandle

RUN_ID = "run-test-0001"

JOB_HANDLE = {
    "run_id": RUN_ID,
    "url": "http://192.168.0.9:8000",
    "namespace": COMPANY_NAMESPACE,
    "job_name": "poseidon-sbx-run-test-0001",
    "pod_name": "poseidon-sbx-run-test-0001-abcde",
}

# What an applied hello-saas blueprint records: the three run-shaped
# workflows, a declared contract tree, and a heartbeat attempt bound.
APPLIED_SPEC = {
    "namespace": COMPANY_NAMESPACE,
    "repo": {"url": "github.com/example/repo", "base_ref": "main"},
    "documents": {"strategy": "docs/STRATEGY.md", "architecture": "docs/ARCHITECTURE.md"},
    "budgets": {"per_run_usd": 5.0, "deadline_minutes": 60},
    "policies": {
        "contract_tests": {
            "paths": ["tests/contract", "tests/acceptance"],
            "roles": ["contract-author"],
        }
    },
    "heartbeat": {"conductor": "pm", "max_attempts": 2},
    "workflows": [
        {"name": "implement", "crew": "feature-delivery", "max_verify_retries": 2},
        {
            "name": "review",
            "crew": "feature-review",
            "gates": {"merge": {"deadline_minutes": 1440}},
        },
    ],
}

PM = {
    "agent_id": "pm-1",
    "role": "pm",
    "url": f"http://pm.{COMPANY_NAMESPACE}.svc.cluster.local:8000",
}


class DestroyOnlyBackend:
    """Teardown's side of the sandbox backend: destroy, and remember it."""

    def __init__(self) -> None:
        self.destroyed: list[str] = []
        self.destroy_error: Exception | None = None

    def destroy(self, handle: SandboxHandle) -> None:
        if self.destroy_error is not None:
            raise self.destroy_error
        self.destroyed.append(handle.job_name)


@pytest.fixture
def backend() -> DestroyOnlyBackend:
    return DestroyOnlyBackend()


@pytest.fixture
def runs() -> InMemoryRuns:
    return InMemoryRuns()


@pytest.fixture
def companies() -> InMemoryCompanies:
    store = InMemoryCompanies()
    store.upsert("hello-saas", APPLIED_SPEC)
    return store


@pytest.fixture
def instances() -> InMemoryAgentInstances:
    store = InMemoryAgentInstances()
    store.upsert("pm-1", "hello-saas", "pm", "pm", namespace=COMPANY_NAMESPACE)
    return store


@pytest.fixture
def ledger() -> InMemoryBudgetLedger:
    return InMemoryBudgetLedger()


@pytest.fixture
def audit() -> InMemoryAuditStore:
    return InMemoryAuditStore()


@pytest.fixture
def activities(settings, backend, runs, companies, instances, ledger, audit):
    return RunActivities(settings, backend, runs, companies, instances, ledger, audit)


# -- create_run ---------------------------------------------------------------


def test_create_run_opens_a_running_row(activities, runs):
    asyncio.run(activities.create_run(RUN_ID, "implement", "do the thing"))
    record = runs.get(RUN_ID)
    assert record.status == "running"
    assert record.task == "do the thing"
    assert record.workflow == "implement"
    assert record.company == "hello-saas"
    assert record.finished_at is None
    # The workflow id derives from the run id and is recorded with it.
    assert record.workflow_id == f"implement-{RUN_ID}"


def test_create_run_names_the_owning_workflow(activities, runs):
    asyncio.run(activities.create_run(RUN_ID, "review", "review pull request #41"))
    assert runs.get(RUN_ID).workflow == "review"
    assert runs.get(RUN_ID).workflow_id == f"review-{RUN_ID}"


def test_exhausted_notice_names_a_human_act_only_where_one_is_left():
    """An abandoned issue or pull request is closed, so its notice names
    no verb a closed item could not take; a bootstrap ticket stays open,
    and its notice names the CLI verb that exists."""
    assert "poseidon start" not in attempts_exhausted_comment("implement", 2, 2)
    assert "closed as not planned" in attempts_exhausted_comment("implement", 2, 2)
    assert "closed unmerged" in attempts_exhausted_comment("review", 1, 1)
    assert "`poseidon start implement <number>`" in attempts_exhausted_comment(
        "implement", 2, 2, bootstrap=True
    )


# -- check_budget -------------------------------------------------------------


def test_budget_boundary_fails_born_exhausted_runs(activities, ledger):
    headroom = asyncio.run(activities.check_budget(RUN_ID))
    assert headroom == {"cap_usd": 5.0, "spent_usd": 0.0}

    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="pm-1",
        parent_agent_id="",
        role="pm",
        model="mock-brain",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        reasoning_tokens=0,
        usd=5.0,
    )
    with pytest.raises(RuntimeError, match="budget_exhausted"):
        asyncio.run(activities.check_budget(RUN_ID))


# -- workflow_config ----------------------------------------------------------


def test_workflow_config_is_the_blueprint_block_plus_the_conductor(activities):
    """Gates, crew, the contract tree, the bounds and the conductor all
    come from the applied spec; the conductor's address is derived from
    its converged instance (the reconciler names the Service after the
    hire), never configured."""
    assert asyncio.run(activities.workflow_config("implement")) == {
        "company": "hello-saas",
        "conductor": PM,
        "crew": "feature-delivery",
        "gates": {},
        "max_verify_retries": 2,
        "budget_deadline_minutes": 60,
        "contract_paths": ["tests/acceptance", "tests/contract"],
        "bounds": render_bounds(APPLIED_SPEC),
        "timeouts": asdict(Timeouts()),
    }
    review = asyncio.run(activities.workflow_config("review"))
    assert review["crew"] == "feature-review"
    assert review["gates"] == {"merge": {"deadline_minutes": 1440}}
    assert review["max_verify_retries"] is None


def test_scope_has_no_node_and_binds_no_crew(activities):
    """Scope dispatches the conductor directly: nothing under `workflows`
    configures it, and its shape is the conductor alone."""
    config = asyncio.run(activities.workflow_config("scope", True))
    assert config["conductor"] == PM
    assert config["crew"] is None and config["gates"] == {}


def test_workflow_config_carries_no_contract_paths_when_none_are_declared(
    settings, backend, runs, instances, ledger, audit
):
    companies = InMemoryCompanies()
    companies.upsert(
        "hello-saas",
        {
            "repo": {"url": "github.com/example/repo", "base_ref": "main"},
            "documents": APPLIED_SPEC["documents"],
            "policies": {},
            "budgets": {"per_run_usd": 5.0, "deadline_minutes": 60},
            "heartbeat": {"conductor": "pm", "max_attempts": 2},
            "workflows": [{"name": "review", "crew": "feature-review"}],
        },
    )
    activities = RunActivities(
        settings, backend, runs, companies, instances, ledger, audit
    )
    config = asyncio.run(activities.workflow_config("review"))
    assert config["contract_paths"] == [] and config["gates"] == {}
    assert "Contract tree: none declared." in config["bounds"]


def test_workflow_config_fails_closed_without_a_blueprint(
    settings, backend, runs, instances, ledger, audit
):
    activities = RunActivities(
        settings, backend, runs, InMemoryCompanies(), instances, ledger, audit
    )
    with pytest.raises(RuntimeError, match="no company is applied"):
        asyncio.run(activities.workflow_config("implement"))


def test_workflow_config_fails_closed_without_a_conductor_instance(
    settings, backend, runs, companies, ledger, audit
):
    """The blueprint names a conductor, but no standing instance has
    converged for it — refuse before anything is provisioned."""
    activities = RunActivities(
        settings, backend, runs, companies, InMemoryAgentInstances(), ledger, audit
    )
    with pytest.raises(RuntimeError, match="no standing instance"):
        asyncio.run(activities.workflow_config("implement"))


# -- record_gate_outcome ------------------------------------------------------


def test_dispatch_documents_hands_a_run_the_present_ones(
    activities, monkeypatch, fake_gateway
):
    """A dispatch carries the documents present on base_ref by name, read
    under the run's own id — the base_ref copy, never a sandbox's."""
    import orchestrator.activities.run as module

    reads = []

    async def read_document(self, run_id, repo, ref, path):
        reads.append((run_id, repo, ref, path))
        present = path == "docs/STRATEGY.md"
        return {"present": present, "text": "Ship slugs." if present else None}

    monkeypatch.setattr(
        module, "GatewayClient", fake_gateway(read_document=read_document)
    )
    documents = asyncio.run(activities.dispatch_documents(RUN_ID))
    assert documents == {"strategy": "Ship slugs."}
    assert reads == [
        (RUN_ID, "github.com/example/repo", "main", "docs/STRATEGY.md"),
        (RUN_ID, "github.com/example/repo", "main", "docs/ARCHITECTURE.md"),
    ]


def test_gate_outcomes_are_process_gate_audit_events(activities, audit):
    """Escalation and the verdict are first-class rows carrying the
    workflow id that maps back to run_id — approved is the only allow."""
    for kind in ("escalated", "approved"):
        asyncio.run(
            activities.record_gate_outcome(RUN_ID, "merge", kind, f"review-{RUN_ID}")
        )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.rule, e.decision) for e in events] == [
        ("gate_escalated", "deny"),
        ("gate_approved", "allow"),
    ]
    assert all(e.source == "process-gate" for e in events)
    assert all(e.tool == gate_event(MERGE_GATE) for e in events)
    assert all(e.workflow == f"review-{RUN_ID}" for e in events)


def test_automation_gate_outcomes_map_to_decisions(activities, audit):
    """automated released a merge (allow); automation_declined fell back
    to the human signal (deny) — distinguishable from human verdicts
    forever."""
    for kind in ("automated", "automation_declined"):
        asyncio.run(
            activities.record_gate_outcome(RUN_ID, "merge", kind, f"review-{RUN_ID}")
        )
    events = [e for e in audit.events if e.run_id == RUN_ID]
    assert [(e.rule, e.decision) for e in events] == [
        ("gate_automated", "allow"),
        ("gate_automation_declined", "deny"),
    ]


def test_unknown_gate_outcome_is_refused_before_it_is_written(activities, audit):
    """The gate vocabulary is `common.workflows`': a kind outside it
    never reaches the ledger."""
    with pytest.raises(ValueError, match="not a gate outcome"):
        asyncio.run(
            activities.record_gate_outcome(
                RUN_ID, "merge", "timed_out", f"review-{RUN_ID}"
            )
        )
    assert [e for e in audit.events if e.run_id == RUN_ID] == []


# -- vacate and teardown ------------------------------------------------------


@pytest.fixture
def unregistered(monkeypatch, fake_gateway) -> list[str]:
    """The gateway's side of a give-back: the runs whose sandbox
    registration was retired."""
    import orchestrator.activities.run as module

    retired: list[str] = []

    async def unregister_sandbox(self, run_id):
        retired.append(run_id)

    monkeypatch.setattr(
        module, "GatewayClient", fake_gateway(unregister_sandbox=unregister_sandbox)
    )
    return retired


@pytest.mark.parametrize("keep_slot", [True, False])
def test_vacate_gives_back_the_crew_the_sandbox_and_the_place_unless_kept(
    settings,
    backend,
    runs,
    companies,
    instances,
    ledger,
    audit,
    unregistered,
    keep_slot,
):
    """A run about to wait on a person gives back what it holds: its
    crew's identities, its sandbox and the sandbox's registration — and
    its place under `max_concurrent_runs`, unless it keeps it. The run
    stays running: only teardown finalizes it."""
    participants = InMemoryRunParticipants()
    crew = CrewActivities(settings, participants, audit, companies, ledger, runs)
    activities = RunActivities(
        settings, backend, runs, companies, instances, ledger, audit, crew
    )
    asyncio.run(activities.create_run(RUN_ID, "review", "review pull request #41"))
    reviewer = "feature-review-reviewer-" + RUN_ID
    participants.add(reviewer, RUN_ID, "pm-1", "feature-review", "reviewer")

    asyncio.run(activities.vacate(RUN_ID, dict(JOB_HANDLE), keep_slot))

    assert participants.active(reviewer) is None
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert unregistered == [RUN_ID]
    run = runs.get(RUN_ID)
    assert (run.status, run.occupies) == ("running", keep_slot)


def test_a_vacate_that_fails_keeps_the_place_and_says_so(
    activities, backend, runs, unregistered
):
    """A give-back that failed frees nothing more: the activity raises,
    the workflow keeps the handle, and teardown tries again."""
    asyncio.run(activities.create_run(RUN_ID, "review", "review pull request #41"))
    backend.destroy_error = RuntimeError("apiserver gone")
    with pytest.raises(RuntimeError, match="apiserver gone"):
        asyncio.run(activities.vacate(RUN_ID, dict(JOB_HANDLE), False))
    assert runs.get(RUN_ID).occupies is True


def test_teardown_destroys_and_finalizes_done(activities, backend, runs, unregistered):
    asyncio.run(activities.create_run(RUN_ID, "implement", "task"))
    result = {"status": "done", "pr_url": "https://pr", "iterations": 3, "error": None}
    record = asyncio.run(activities.teardown(RUN_ID, dict(JOB_HANDLE), result, None))
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert unregistered == [RUN_ID]
    assert record["status"] == "done"
    assert record["pr_url"] == "https://pr"
    assert record["iterations"] == 3
    stored = runs.get(RUN_ID)
    assert stored.status == "done"
    assert stored.finished_at is not None


def test_teardown_records_error_and_still_destroys(activities, backend, unregistered):
    asyncio.run(activities.create_run(RUN_ID, "implement", "task"))
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), None, "dispatch_agent: boom")
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert unregistered == [RUN_ID]
    assert record["status"] == "failed"
    assert record["error"] == "dispatch_agent: boom"


def test_teardown_without_sandbox_finalizes_failed(activities, backend):
    asyncio.run(activities.create_run(RUN_ID, "implement", "task"))
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "fetch_workspace: 500"))
    assert backend.destroyed == []
    assert record["status"] == "failed"


def test_teardown_links_the_tickets_a_run_filed(activities, runs):
    """A scope run's artifacts are tickets, not a PR: the run record carries
    them the way an implement record carries its pr_url."""
    asyncio.run(activities.create_run(RUN_ID, "scope", "scope the next slice"))
    result = {
        "status": "done",
        "ticket_urls": ["https://github.test/i/7"],
        "iterations": 0,
        "error": None,
    }
    record = asyncio.run(activities.teardown(RUN_ID, None, result, None))
    assert record["status"] == "done"
    assert record["ticket_urls"] == ["https://github.test/i/7"]
    assert runs.get(RUN_ID).ticket_urls == ["https://github.test/i/7"]


def test_teardown_surfaces_destroy_failure_as_error(activities, backend, unregistered):
    """A destroy that failed is the run's error; the registration is
    retired all the same."""
    asyncio.run(activities.create_run(RUN_ID, "implement", "task"))
    backend.destroy_error = RuntimeError("apiserver gone")
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), {"status": "done"}, None)
    )
    assert record["status"] == "failed"
    assert "apiserver gone" in record["error"]
    assert unregistered == [RUN_ID]


def test_teardown_reconstructs_a_missing_run_row(activities):
    """A fresh database mid-run must not break compensation."""
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "row vanished"))
    assert record["status"] == "failed"
    assert record["run_id"] == RUN_ID


def test_teardown_revokes_participants_on_every_terminal_path(
    settings, backend, runs, companies, instances, ledger, audit, unregistered
):
    """Run-binding termination rides the one compensation anchor: the
    sweep runs before the sandbox destroy, on the failure path (no handle)
    and the success path alike."""
    participants = InMemoryRunParticipants()
    crew = CrewActivities(settings, participants, audit, companies, ledger, runs)
    activities = RunActivities(
        settings, backend, runs, companies, instances, ledger, audit, crew
    )

    participants.add(
        "feature-delivery-implementer-" + RUN_ID,
        RUN_ID,
        "pm-1",
        "feature-delivery",
        "implementer",
    )
    record = asyncio.run(activities.teardown(RUN_ID, None, None, "early failure"))
    assert record["status"] == "failed"
    assert participants.active("feature-delivery-implementer-" + RUN_ID) is None
    assert [e.tool for e in audit.events] == [LIFECYCLE_REVOKE]
    assert unregistered == []  # no sandbox, no registration to retire

    # Success path, with a handle: sweep still runs first.
    participants.add(
        "feature-review-reviewer-" + RUN_ID,
        RUN_ID,
        "pm-1",
        "feature-review",
        "reviewer",
    )
    record = asyncio.run(
        activities.teardown(
            RUN_ID, dict(JOB_HANDLE), {"status": "done", "pr_url": "https://pr"}, None
        )
    )
    assert record["status"] == "done"
    assert participants.active("feature-review-reviewer-" + RUN_ID) is None
    assert unregistered == [RUN_ID]


def test_revoke_failure_is_loud_but_not_masking(
    settings, backend, runs, companies, instances, ledger, audit, unregistered
):
    """A failed sweep surfaces in the run record; the sandbox is still
    destroyed and the run still finalizes."""

    class BrokenCrew:
        async def revoke_participants(self, run_id: str):
            raise RuntimeError("registry unreachable")

    activities = RunActivities(
        settings, backend, runs, companies, instances, ledger, audit, BrokenCrew()
    )
    record = asyncio.run(
        activities.teardown(RUN_ID, dict(JOB_HANDLE), {"status": "done"}, None)
    )
    assert backend.destroyed == [JOB_HANDLE["job_name"]]
    assert unregistered == [RUN_ID]
    assert record["status"] == "failed"
    assert "ephemeral agent revoke failed" in record["error"]


# -- abandon_exhausted ---------------------------------------------------------


def _exhaustion_gateway(
    monkeypatch, fake_gateway, issues: dict[int, dict[str, Any]] | None = None
) -> list[tuple[str, int, str]]:
    """The world's side of the abandonment: the issues the host serves by
    number (open unless stated), and every act the activity takes, as
    (act, number, text) — `close_issue` also carries its reason."""
    import orchestrator.activities.run as module

    acts: list[tuple[str, int, str]] = []
    served = issues or {}

    async def read_issue(self, run_id, repo, number):
        return served.get(number) or {"number": number, "title": "t", "state": "open"}

    async def read_open_issues(self, run_id, repo):
        return [i for i in served.values() if i.get("state") == "open"]

    async def close_issue(self, run_id, repo, number, reason, rationale):
        acts.append((f"close_issue {reason}", number, rationale))
        return {"number": number, "state": "closed"}

    async def close_pr(self, run_id, repo, number, rationale, delete_branch):
        acts.append(("close_pr", number, rationale))
        return {"number": number, "state": "closed"}

    async def comment_issue(self, run_id, repo, number, body):
        acts.append(("comment", number, body))
        return {}

    monkeypatch.setattr(
        module,
        "GatewayClient",
        fake_gateway(
            read_issue=read_issue,
            read_open_issues=read_open_issues,
            close_issue=close_issue,
            close_pr=close_pr,
            comment_issue=comment_issue,
        ),
    )
    return acts


def _aborted(runs: InMemoryRuns, run_id: str, task: str, workflow: str) -> None:
    runs.start(run_id, task, workflow, company="hello-saas")
    runs.finish(run_id, "failed", error="litellm.Timeout")


def test_the_last_aborted_review_records_exhaustion_and_refuses_the_pr(
    activities, runs, audit, monkeypatch, fake_gateway
):
    """Two reviews of one PR aborted without a verdict under
    `max_attempts: 2`: the second is the last the blueprint allows, so it
    writes the process-gate event under its own run id and refuses the
    delivery, the notice on the PR — and the ticket the PR delivered,
    with an attempt left, is told it is back on the backlog."""
    acts = _exhaustion_gateway(monkeypatch, fake_gateway)
    runs.start("run-impl", "implement issue #3", "implement", company="hello-saas")
    runs.finish("run-impl", "done", pr_url="https://git.test/r/pull/7")
    _aborted(runs, "run-r1", "review pull request #7", "review")
    _aborted(runs, "run-r2", "review pull request #7", "review")

    out = asyncio.run(activities.abandon_exhausted("run-r2", "review-run-r2"))

    assert out == {"exhausted": True, "attempts": 2}
    (event,) = audit.story("run-r2")
    assert event["tool"] == gate_event(ATTEMPTS_GATE)
    assert event["rule"] == "gate_exhausted"
    assert event["decision"] == "deny"
    assert event["source"] == "process-gate"
    assert event["workflow"] == "review-run-r2"
    assert audit.story("run-r1") == []
    (closed, told) = acts
    assert closed[:2] == ("close_pr", 7)
    assert "none reached a verdict" in closed[2] and "max_attempts" in closed[2]
    assert told == ("comment", 3, refused_comment(7, closed[2], 1, 2, False))
    assert runs.get("run-impl").pr_settled == "refused"


def test_the_last_aborted_issue_run_closes_the_issue_as_not_planned(
    activities, runs, audit, monkeypatch, fake_gateway
):
    acts = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-i1", "implement issue #3", "implement")
    _aborted(runs, "run-i2", "implement issue #3", "implement")

    out = asyncio.run(activities.abandon_exhausted("run-i2", "implement-run-i2"))

    assert out["exhausted"] is True
    assert [e["rule"] for e in audit.story("run-i2")] == ["gate_exhausted"]
    ((act, number, notice),) = acts
    assert (act, number) == ("close_issue not_planned", 3)
    assert notice == (
        "Poseidon spent 2 attempt(s) on this issue and none delivered work that"
        " merged. That is the blueprint's `heartbeat.max_attempts` (2): the"
        " ticket is closed as not planned."
    )


def test_an_abandoned_rework_ticket_closes_its_chain(
    activities, runs, audit, monkeypatch, fake_gateway
):
    """A rework ticket's chain goes with it: the root ticket the root PR
    delivered, and every open rework ticket filed for that root PR."""
    acts = _exhaustion_gateway(
        monkeypatch,
        fake_gateway,
        {
            5: {"number": 5, "title": "Add slugify", "state": "open"},
            9: {"number": 9, "title": "Rework 1 for PR #6: Add slugify", "state": "open"},
            11: {
                "number": 11,
                "title": "Rework 2 for PR #6: Add slugify",
                "state": "open",
            },
            12: {"number": 12, "title": "Unrelated", "state": "open"},
        },
    )
    runs.start("run-root", "implement issue #5", "implement", company="hello-saas")
    runs.finish("run-root", "done", pr_url="https://git.test/r/pull/6")
    _aborted(runs, "run-i1", "implement issue #11", "implement")
    _aborted(runs, "run-i2", "implement issue #11", "implement")

    asyncio.run(activities.abandon_exhausted("run-i2", "implement-run-i2"))

    assert [(act, number) for act, number, _ in acts] == [
        ("close_issue not_planned", 5),
        ("close_issue not_planned", 9),
        ("close_issue not_planned", 11),
    ]


def test_an_exhausted_bootstrap_ticket_is_told_not_closed(
    activities, runs, audit, monkeypatch, fake_gateway
):
    """Closing a bootstrap ticket would have the heartbeat file it again:
    the heartbeat's halt is its record, and the ticket only gets the
    notice, with the one human act left."""
    acts = _exhaustion_gateway(
        monkeypatch,
        fake_gateway,
        {3: {"number": 3, "title": "Bootstrap: strategy", "state": "open"}},
    )
    _aborted(runs, "run-i1", "implement issue #3", "implement")
    _aborted(runs, "run-i2", "implement issue #3", "implement")

    asyncio.run(activities.abandon_exhausted("run-i2", "implement-run-i2"))

    ((act, number, notice),) = acts
    assert (act, number) == ("comment", 3)
    assert notice.endswith(
        "the heartbeat halts on this document's ticket. A human can:"
        " `poseidon start implement <number>`."
    )


def test_an_abort_with_attempts_left_records_nothing(
    activities, runs, audit, monkeypatch, fake_gateway
):
    """The first abort is not an abandonment: the item goes back on the
    backlog, and nothing is written until the cap is actually spent."""
    acts = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-i1", "implement issue #3", "implement")

    out = asyncio.run(activities.abandon_exhausted("run-i1", "implement-run-i1"))

    assert out == {"exhausted": False}
    assert audit.story("run-i1") == [] and acts == []


def test_a_concluded_item_is_consumed_by_its_conclusion_not_by_exhaustion(
    activities, runs, audit, monkeypatch, fake_gateway
):
    """A review that reached a verdict concluded the PR whatever the gate
    did next; a later abort on the same PR (a human's re-run) spends no
    heartbeat attempt and announces no exhaustion."""
    acts = _exhaustion_gateway(monkeypatch, fake_gateway)
    runs.start("run-r1", "review pull request #7", "review", company="hello-saas")
    runs.record_verdict("run-r1", "fail")
    runs.finish("run-r1", "failed", error="merge gate rejected")
    _aborted(runs, "run-r2", "review pull request #7", "review")

    out = asyncio.run(activities.abandon_exhausted("run-r2", "review-run-r2"))

    assert out == {"exhausted": False}
    assert audit.story("run-r2") == [] and acts == []


def test_a_scope_run_has_no_item_to_exhaust(
    activities, runs, audit, monkeypatch, fake_gateway
):
    """A scope run works no ticket and no PR: however it ended, nothing
    is offered again and nothing is exhausted."""
    acts = _exhaustion_gateway(monkeypatch, fake_gateway)
    _aborted(runs, "run-s1", "scope the next slice", "scope")
    _aborted(runs, "run-s2", "scope the next slice", "scope")

    out = asyncio.run(activities.abandon_exhausted("run-s2", "scope-run-s2"))

    assert out == {"exhausted": False}
    assert audit.story("run-s2") == [] and acts == []
