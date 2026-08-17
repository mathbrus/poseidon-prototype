"""What the registry must guarantee for the services that depend on it."""

from datetime import UTC, datetime

import pytest

from common.audit import AuditEvent
from registry.audit import AuditStore
from registry.db import Registry
from registry.runs import Runs
from registry.sandboxes import SandboxRegistrations

RUN_ID = "run-01REGISTRYTEST"


def event(**overrides) -> AuditEvent:
    fields = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": RUN_ID,
        "agent_id": "backend-engineer-1",
        "role": "backend-engineer",
        "tool": "sandbox.exec",
        "args_digest": "a" * 64,
        "decision": "allow",
        "rule": "sandbox_assigned_run",
        "latency_ms": 3,
        "backend_status": "ok",
        "source": "gateway",
        "company": "hello-saas",
        "lifecycle": "standing",
        "workflow": "implement",
    }
    fields.update(overrides)
    return AuditEvent(**fields)


# -- migration ------------------------------------------------------------


def test_migrate_is_idempotent(registry: Registry):
    registry.migrate()
    registry.migrate()  # every service runs this at every startup


# -- runs -----------------------------------------------------------------


def test_run_lifecycle(registry: Registry):
    runs = Runs(registry)
    runs.start(RUN_ID, "do the thing", workflow_id=f"implement-{RUN_ID}")
    opened = runs.get(RUN_ID)
    assert opened.status == "running"
    assert opened.workflow_id == f"implement-{RUN_ID}"
    assert opened.finished_at is None

    record = runs.finish(RUN_ID, "done", pr_url="https://pr/1", iterations=2)
    assert record.status == "done"
    assert record.pr_url == "https://pr/1"
    assert record.finished_at is not None


def test_start_is_retry_safe(registry: Registry):
    """A re-attempt after a worker death must land on the same row, and
    must clear a terminal state left by a previous attempt."""
    runs = Runs(registry)
    runs.start(RUN_ID, "attempt one")
    runs.finish(RUN_ID, "failed", error="boom")
    runs.start(RUN_ID, "attempt two")

    record = runs.get(RUN_ID)
    assert record.task == "attempt two"
    assert record.status == "running"
    assert record.error is None and record.finished_at is None
    assert len(runs.recent()) == 1


def test_finish_recreates_a_missing_row(registry: Registry):
    record = Runs(registry).finish(RUN_ID, "failed", error="row vanished")
    assert record.run_id == RUN_ID and record.status == "failed"


def test_record_artifacts_accumulates(registry: Registry):
    runs = Runs(registry)
    runs.start(RUN_ID, "task")
    runs.record_artifacts(RUN_ID, ticket_urls=["https://issues/1"])
    runs.record_artifacts(RUN_ID, pr_url="https://pr/1")
    record = runs.get(RUN_ID)
    assert record.ticket_urls == ["https://issues/1"]  # not clobbered
    assert record.pr_url == "https://pr/1"


# -- sandbox registrations ------------------------------------------------


def test_registration_survives_a_new_accessor(registry: Registry):
    """The done-when behind resolution row 16: the address outlives the
    process that recorded it, so a gateway restart mid-run is survivable."""
    SandboxRegistrations(registry).register(
        RUN_ID, "http://10.0.0.1:8000", namespace="hello-saas", job_name="sbx"
    )
    # A brand-new accessor stands in for the restarted gateway.
    assert SandboxRegistrations(registry).url_for(RUN_ID) == "http://10.0.0.1:8000"


def test_reprovision_replaces_the_address(registry: Registry):
    sandboxes = SandboxRegistrations(registry)
    sandboxes.register(RUN_ID, "http://10.0.0.1:8000")
    sandboxes.register(RUN_ID, "http://10.0.0.2:8000")
    assert sandboxes.url_for(RUN_ID) == "http://10.0.0.2:8000"


def test_terminated_registration_is_not_served(registry: Registry):
    """A call against a torn-down sandbox must find nothing, not whatever
    now answers at that address."""
    sandboxes = SandboxRegistrations(registry)
    sandboxes.register(RUN_ID, "http://10.0.0.1:8000")
    sandboxes.terminate(RUN_ID)
    assert sandboxes.url_for(RUN_ID) is None


def test_terminate_is_idempotent(registry: Registry):
    sandboxes = SandboxRegistrations(registry)
    sandboxes.register(RUN_ID, "http://10.0.0.1:8000")
    sandboxes.terminate(RUN_ID)
    sandboxes.terminate(RUN_ID)
    assert sandboxes.url_for(RUN_ID) is None


def test_reregistration_after_teardown_revives_the_row(registry: Registry):
    sandboxes = SandboxRegistrations(registry)
    sandboxes.register(RUN_ID, "http://10.0.0.1:8000")
    sandboxes.terminate(RUN_ID)
    sandboxes.register(RUN_ID, "http://10.0.0.3:8000")
    assert sandboxes.url_for(RUN_ID) == "http://10.0.0.3:8000"


# -- audit ----------------------------------------------------------------


def test_story_is_one_run_in_order(registry: Registry):
    audit = AuditStore(registry)
    audit.append(event(tool="repo.archive", ts="2026-01-01T00:00:01+00:00"))
    audit.append(event(tool="sandbox.exec", ts="2026-01-01T00:00:02+00:00"))
    audit.append(event(run_id="run-other", tool="git.open_pr"))

    story = audit.story(RUN_ID)
    assert [e["tool"] for e in story] == ["repo.archive", "sandbox.exec"]


def test_story_spans_all_four_sources(registry: Registry):
    """Gateway, sandbox-infra, llm-gateway and process-gate events for one
    run answer a single `run_id`-correlated query, attribution included."""
    audit = AuditStore(registry)
    audit.append(event(tool="sandbox.exec", ts="2026-01-01T00:00:01+00:00"))
    audit.append(
        event(
            agent_id="egress-proxy",
            role="",
            source="sandbox-infra",
            tool="sandbox.egress",
            decision="deny",
            rule="egress_not_allowlisted",
            backend_status=None,
            ts="2026-01-01T00:00:02+00:00",
        )
    )
    audit.append(
        event(
            agent_id="backend-engineer-1",
            source="llm-gateway",
            tool="llm.complete",
            lifecycle="standing",
            ts="2026-01-01T00:00:03+00:00",
        )
    )
    audit.append(
        event(
            agent_id="orchestrator",
            role="platform",
            lifecycle="platform",
            source="process-gate",
            tool="gate.merge",
            rule="gate_approved",
            ts="2026-01-01T00:00:04+00:00",
        )
    )

    story = audit.story(RUN_ID)
    assert [e["source"] for e in story] == [
        "gateway",
        "sandbox-infra",
        "llm-gateway",
        "process-gate",
    ]
    assert all(e["run_id"] == RUN_ID for e in story)
    assert story[0]["company"] == "hello-saas"
    assert story[0]["workflow"] == "implement"


def test_unattributable_attempts_are_still_recorded(registry: Registry):
    """An event whose run_id is an unverified claim is exactly the one worth
    keeping — hence no foreign key on run_id."""
    audit = AuditStore(registry)
    audit.append(
        event(
            run_id="run-does-not-exist",
            decision="deny",
            rule="egress_bad_credential",
            backend_status=None,
        )
    )
    assert len(audit.story("run-does-not-exist")) == 1


# -- the operator-owned tables ----------------------------------------------


def test_policy_bundle_round_trips_and_revokes(registry: Registry):
    from registry.bundles import PolicyBundles

    bundles = PolicyBundles(registry)
    revision = bundles.put("test-corp", {"authz.rego": "package poseidon.authz\n"})
    stored = bundles.get("test-corp")
    assert stored is not None and stored.revision == revision
    assert stored.tarball  # the bytea comes back as bytes

    bundles.delete("test-corp")
    assert bundles.get("test-corp") is None


def test_company_rows_and_instance_sweep(registry: Registry):
    from registry.bundles import AgentInstances, Companies

    companies, instances = Companies(registry), AgentInstances(registry)
    companies.upsert("test-corp", {"namespace": "test-corp", "mission": "m"})
    instances.upsert("tc-pm-1", "test-corp", "pm", "pm", "standard")
    instances.upsert("other-1", "other-corp", "pm", "pm", "standard")
    assert [r["agent_id"] for r in instances.for_company("test-corp")] == ["tc-pm-1"]

    # The teardown sweep: company-scoped, other companies untouched.
    instances.delete_for_company("test-corp")
    companies.delete("test-corp")
    assert instances.for_company("test-corp") == []
    assert [r["agent_id"] for r in instances.for_company("other-corp")] == ["other-1"]
    instances.delete_for_company("other-corp")


# -- workload identity reads ------------------------------------------------


def test_agent_instance_lookup_carries_namespace(registry: Registry):
    from registry.bundles import AgentInstances

    instances = AgentInstances(registry)
    instances.upsert(
        "backend-engineer-1",
        "hello-saas",
        "backend-engineer",
        "backend-engineer",
        "standard",
        namespace="hello-saas",
    )
    try:
        row = instances.get("backend-engineer-1")
        assert row["namespace"] == "hello-saas"
        assert row["role_template"] == "backend-engineer"
        assert instances.get("nobody") is None
    finally:
        instances.delete("backend-engineer-1")


def test_participant_active_goes_dark_on_termination(registry: Registry):
    """Revocation-before-TTL rides on this row disappearing from `active`."""
    from registry.participants import RunParticipants

    Runs(registry).start(RUN_ID, "task")
    registry.execute(
        """
        INSERT INTO registry.run_participants
            (agent_id, run_id, parent_agent_id, crew_template, role)
        VALUES (%s, %s, %s, %s, %s)
        """,
        ("executor-01X", RUN_ID, "pm-1", "feature-delivery", "backend-engineer"),
    )
    participants = RunParticipants(registry)
    assert participants.active("executor-01X")["run_id"] == RUN_ID

    registry.execute(
        "UPDATE registry.run_participants"
        " SET status = 'terminated', terminated_at = now()"
        " WHERE agent_id = %s",
        ("executor-01X",),
    )
    assert participants.active("executor-01X") is None


def test_sandbox_registration_active_row(registry: Registry):
    """The run-binding read: live row with namespace, gone
    once terminated — a stale run_id claim must resolve to nothing."""
    boxes = SandboxRegistrations(registry)
    boxes.register(RUN_ID, "http://10.0.0.5:8000", "hello-saas", "sbx-x")
    row = boxes.active(RUN_ID)
    assert row["namespace"] == "hello-saas"
    assert row["url"] == "http://10.0.0.5:8000"

    boxes.terminate(RUN_ID)
    assert boxes.active(RUN_ID) is None
    assert boxes.url_for(RUN_ID) is None


def test_company_spec_reads(registry: Registry):
    from registry.bundles import Companies

    companies = Companies(registry)
    companies.upsert(
        "hello-saas", {"namespace": "hello-saas", "infra": {"sandbox": {"cpu": "1"}}}
    )
    try:
        assert companies.get("hello-saas")["infra"]["sandbox"]["cpu"] == "1"
        assert companies.by_namespace("hello-saas")["namespace"] == "hello-saas"
        assert companies.get("nobody") is None
        assert companies.by_namespace("nowhere") is None
    finally:
        companies.delete("hello-saas")


def test_budget_ledger_attributes_and_sums(registry: Registry):
    """One ledger with dimensions: every entry fully attributed,
    the per-run SUM is what the LLM gateway's hard-stop compares against."""
    from registry.ledger import BudgetLedger

    ledger = BudgetLedger(registry)
    Runs(registry).start(RUN_ID, "task")
    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="backend-engineer-1",
        parent_agent_id="",
        role="backend-engineer",
        model="mock-brain",
        prompt_tokens=100,
        completion_tokens=50,
        usd=0.0003,
    )
    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="executor-01X",
        parent_agent_id="pm-1",
        role="backend-engineer",
        model="mock-brain",
        prompt_tokens=10,
        completion_tokens=5,
        usd=0.0001,
    )
    registry.execute("DELETE FROM registry.budget_ledger WHERE run_id != %s", (RUN_ID,))
    try:
        assert abs(ledger.spent_for_run(RUN_ID) - 0.0004) < 1e-9
        entries = ledger.entries_for_run(RUN_ID)
        assert [e["agent_id"] for e in entries] == ["backend-engineer-1", "executor-01X"]
        assert entries[1]["parent_agent_id"] == "pm-1"
        assert ledger.spent_for_run("run-unmetered") == 0.0
    finally:
        registry.execute(
            "DELETE FROM registry.budget_ledger WHERE run_id = %s", (RUN_ID,)
        )


def test_crew_request_is_idempotent_and_expiring(registry: Registry):
    """Same {conductor, crew, run} lands on one row;
    while recorded a retry refreshes it; once fulfilled it is untouched —
    a retry must not resurrect consumed intent."""
    from registry.crews import CrewRequests

    Runs(registry).start(RUN_ID, "task")
    requests = CrewRequests(registry)
    first = requests.record("pm-1", "feature-delivery", RUN_ID, "ship it")
    try:
        assert first["status"] == "recorded"
        assert first["expires_at"] is not None

        again = requests.record("pm-1", "feature-delivery", RUN_ID, "ship it now")
        assert again["id"] == first["id"]
        assert again["reason"] == "ship it now"

        registry.execute(
            "UPDATE registry.crew_requests SET status='fulfilled',"
            " fulfilled_at=now() WHERE id = %s",
            (first["id"],),
        )
        after = requests.record("pm-1", "feature-delivery", RUN_ID, "retry")
        assert after["status"] == "fulfilled"
        assert after["reason"] == "ship it now"  # untouched
    finally:
        registry.execute(
            "DELETE FROM registry.crew_requests WHERE id = %s", (first["id"],)
        )


def test_participant_bind_is_retry_safe_and_sweepable(registry: Registry):
    """16a contract: a retried bind (same deterministic agent_id) converges
    on the same row; the per-run sweep revokes every live participant at
    once and reports who it swept."""
    from registry.participants import RunParticipants

    Runs(registry).start(RUN_ID, "task")
    participants = RunParticipants(registry)
    try:
        participants.add(
            "feature-delivery-executor-x", RUN_ID, "pm-1", "feature-delivery", "executor"
        )
        # A retry against the live row converges — one identity, never two.
        participants.add(
            "feature-delivery-executor-x", RUN_ID, "pm-1", "feature-delivery", "executor"
        )
        assert participants.active("feature-delivery-executor-x") is not None

        participants.add(
            "feature-delivery-planner-x", RUN_ID, "pm-1", "feature-delivery", "planner"
        )
        swept = participants.terminate_for_run(RUN_ID)
        assert sorted(swept) == [
            "feature-delivery-executor-x",
            "feature-delivery-planner-x",
        ]
        assert participants.active("feature-delivery-planner-x") is None
        assert participants.terminate_for_run(RUN_ID) == []  # idempotent
    finally:
        registry.execute(
            "DELETE FROM registry.run_participants WHERE run_id = %s", (RUN_ID,)
        )


def test_participant_row_resolves_by_service_account(registry: Registry):
    """The row is the whole ephemeral identity: the gateway looks it up by
    ServiceAccount name and reads company, catalog template, namespace and
    the canonical agent_id from it — and the lookup goes dark on
    termination, which is what makes revocation a registry write."""
    from registry.participants import RunParticipants

    Runs(registry).start(RUN_ID, "task")
    participants = RunParticipants(registry)
    try:
        participants.add(
            "feature-delivery-executor-X",
            RUN_ID,
            "pm-1",
            "feature-delivery",
            "executor",
            company="hello-saas",
            role_template="backend-engineer",
            namespace="hello-saas",
            service_account="crew-feature-delivery-executor-x",
        )
        row = participants.by_service_account("crew-feature-delivery-executor-x")
        assert row is not None
        # Lowercasing the SA name is not invertible; the canonical id is
        # read from the row, never derived from the name.
        assert row["agent_id"] == "feature-delivery-executor-X"
        assert row["company"] == "hello-saas"
        assert row["role_template"] == "backend-engineer"
        assert row["namespace"] == "hello-saas"

        participants.terminate("feature-delivery-executor-X")
        assert (
            participants.by_service_account("crew-feature-delivery-executor-x") is None
        )
    finally:
        registry.execute(
            "DELETE FROM registry.run_participants WHERE run_id = %s", (RUN_ID,)
        )


def test_crew_request_consume_and_spawn_count(registry: Registry):
    """16d: consume claims a recorded, unexpired request exactly once (the
    fulfilling run may re-claim its own after a crash); the spawn count is
    distinct live {run, crew} pairs per conductor."""
    from registry.crews import CrewRequests
    from registry.participants import RunParticipants

    Runs(registry).start(RUN_ID, "task")
    requests = CrewRequests(registry)
    participants = RunParticipants(registry)
    recorded = requests.record("pm-1", "feature-delivery", RUN_ID, "go")
    try:
        consumed = requests.consume(recorded["id"], RUN_ID)
        assert consumed["status"] == "fulfilled"
        # Idempotent for the same run; a foreign run gets nothing.
        assert requests.consume(recorded["id"], RUN_ID) is not None
        assert requests.consume(recorded["id"], "run-other") is None
        assert requests.consume("crq-missing", RUN_ID) is None

        participants.add("fd-planner-1", RUN_ID, "pm-1", "feature-delivery", "planner")
        participants.add("fd-executor-1", RUN_ID, "pm-1", "feature-delivery", "executor")
        assert participants.active_spawn_count("pm-1") == 1  # one crew, two members
        participants.add("fr-reviewer-1", RUN_ID, "pm-1", "feature-review", "reviewer")
        assert participants.active_spawn_count("pm-1") == 2
        participants.terminate_for_run(RUN_ID)
        assert participants.active_spawn_count("pm-1") == 0
    finally:
        registry.execute(
            "DELETE FROM registry.crew_requests WHERE id = %s", (recorded["id"],)
        )
        registry.execute(
            "DELETE FROM registry.run_participants WHERE run_id = %s", (RUN_ID,)
        )


def test_revocation_is_final_and_a_late_mint_cannot_revive_it(registry: Registry):
    """The only way to mint onto a terminated row is to be a
    retry that lost the race with its run's teardown. Reviving it would
    resurrect an identity the platform revoked — and, in the case that
    surfaced this, leave a crew pod holding it with nothing left to collect
    the pod. Revocation is final; the late caller is told to stop."""
    from registry.participants import ParticipantRevoked, RunParticipants

    Runs(registry).start(RUN_ID, "task")
    participants = RunParticipants(registry)
    try:
        participants.add(
            "feature-delivery-executor-z", RUN_ID, "pm-1", "feature-delivery", "executor"
        )
        assert participants.terminate_for_run(RUN_ID) == ["feature-delivery-executor-z"]

        with pytest.raises(ParticipantRevoked):
            participants.add(
                "feature-delivery-executor-z",
                RUN_ID,
                "pm-1",
                "feature-delivery",
                "executor",
            )
        # Still revoked: the failed mint changed nothing.
        assert participants.active("feature-delivery-executor-z") is None
        rows = registry.query(
            "SELECT status FROM registry.run_participants WHERE agent_id = %s",
            ("feature-delivery-executor-z",),
        )
        assert rows[0]["status"] == "terminated"
    finally:
        registry.execute(
            "DELETE FROM registry.run_participants WHERE run_id = %s", (RUN_ID,)
        )
        registry.execute("DELETE FROM registry.runs WHERE run_id = %s", (RUN_ID,))
