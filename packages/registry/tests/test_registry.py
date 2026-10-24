"""What the registry must guarantee for the services that depend on it."""

import io
import json
import tarfile
from datetime import UTC, datetime

import pytest

from common.audit import REVIEW_VERDICT, SANDBOX_EGRESS, AuditEvent, gate_event
from common.policy import GATE_RESOLVE, verdict_rule
from common.workflows import MERGE_GATE
from registry import (
    AgentInstances,
    Attempts,
    AuditStore,
    BudgetLedger,
    Companies,
    FiledTickets,
    ParticipantRevoked,
    PolicyBundles,
    Registry,
    RunParticipants,
    Runs,
    SandboxRegistrations,
)

RUN_ID = "run-01REGISTRYTEST"


def event(**overrides) -> AuditEvent:
    fields = {
        "ts": datetime.now(UTC).isoformat(),
        "run_id": RUN_ID,
        "agent_id": "implementer-1",
        "role": "implementer",
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
    runs.start(RUN_ID, "attempt one", company="hello-saas")
    runs.finish(RUN_ID, "failed", error="boom")
    runs.start(RUN_ID, "attempt two", company="hello-saas")

    record = runs.get(RUN_ID)
    assert record.task == "attempt two"
    assert record.status == "running"
    assert record.error is None and record.finished_at is None
    assert len(runs.recent("hello-saas")) == 1


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
    # Tickets accumulate in order, each once: the ticket a run works, then
    # the ones its dispatches file.
    runs.record_artifacts(RUN_ID, ticket_urls=["https://issues/2", "https://issues/1"])
    assert runs.get(RUN_ID).ticket_urls == ["https://issues/1", "https://issues/2"]
    # Once means once within a single write too.
    runs.record_artifacts(RUN_ID, ticket_urls=["https://issues/3", "https://issues/3"])
    assert runs.get(RUN_ID).ticket_urls == [
        "https://issues/1",
        "https://issues/2",
        "https://issues/3",
    ]


# -- sandbox registrations ------------------------------------------------


def test_registration_survives_a_new_accessor(registry: Registry):
    """The durability contract: the address outlives the
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


def test_a_rationale_is_read_back_in_cleartext(registry: Registry):
    """The one field that is not a digest: the actor's stated reason rides
    the row and comes back verbatim from every read; rows that decided
    nothing carry none."""
    audit = AuditStore(registry)
    audit.append(
        event(
            tool=REVIEW_VERDICT,
            rule="verdict_pass",
            rationale="Suite green; the change matches the acceptance criteria.",
            ts="2026-01-01T00:00:01+00:00",
        )
    )
    audit.append(event(tool="sandbox.exec", ts="2026-01-01T00:00:02+00:00"))
    story = audit.story(RUN_ID)
    assert [e["rationale"] for e in story] == [
        "Suite green; the change matches the acceptance criteria.",
        None,
    ]
    assert audit.after("hello-saas", 0)[0]["rationale"] == story[0]["rationale"]
    # The decisions-only read keeps exactly the rows that state a reason.
    assert [e["tool"] for e in audit.story(RUN_ID, decisions_only=True)] == [
        REVIEW_VERDICT
    ]


def test_story_spans_every_source(registry: Registry):
    """Gateway, sandbox-infra, llm-gateway and process-gate events for one
    run answer a single `run_id`-correlated query, attribution included."""
    audit = AuditStore(registry)
    audit.append(event(tool="sandbox.exec", ts="2026-01-01T00:00:01+00:00"))
    audit.append(
        event(
            agent_id="egress-proxy",
            role="",
            source="sandbox-infra",
            tool=SANDBOX_EGRESS,
            decision="deny",
            rule="egress_not_allowlisted",
            backend_status=None,
            ts="2026-01-01T00:00:02+00:00",
        )
    )
    audit.append(
        event(
            agent_id="implementer-1",
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
            tool=gate_event(MERGE_GATE),
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


# -- the reconciler-owned tables ----------------------------------------------


def test_policy_bundle_round_trips_and_revokes(registry: Registry):
    bundles = PolicyBundles(registry)
    revision = bundles.put("test-corp", {"authz.rego": "package poseidon.authz.test\n"})
    stored = bundles.get("test-corp")
    assert stored is not None and stored.revision == revision
    assert stored.tarball  # the bytea comes back as bytes

    # Revoked is still served — as a tombstone over the same roots, so
    # OPA's next poll erases the rules rather than keeping the last ones.
    tombstone_revision = bundles.revoke("test-corp")
    revoked = bundles.get("test-corp")
    assert revoked is not None and revoked.revision == tombstone_revision != revision
    with tarfile.open(fileobj=io.BytesIO(revoked.tarball)) as tar:
        assert tar.getnames() == [".manifest"]
        manifest = json.loads(tar.extractfile(".manifest").read())
    assert manifest["roots"] == ["poseidon/authz/test"]
    # Idempotent: a re-run teardown stores the same tombstone.
    assert bundles.revoke("test-corp") == tombstone_revision


def test_revoking_what_was_never_stored_is_nothing(registry: Registry):
    assert PolicyBundles(registry).revoke("nobody") is None
    assert PolicyBundles(registry).get("nobody") is None


def test_company_rows_and_instance_sweep(registry: Registry):
    companies, instances = Companies(registry), AgentInstances(registry)
    companies.upsert("test-corp", {"namespace": "test-corp", "vision": "m"})
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
    instances = AgentInstances(registry)
    instances.upsert(
        "implementer-1",
        "hello-saas",
        "implementer",
        "implementer",
        namespace="hello-saas",
    )
    row = instances.get("implementer-1")
    assert row["namespace"] == "hello-saas"
    assert row["standing_name"] == "implementer"
    assert row["role"] == "implementer"
    assert instances.get("nobody") is None


def test_participant_active_goes_dark_on_termination(registry: Registry):
    """Revocation-before-TTL rides on this row disappearing from `active`."""
    Runs(registry).start(RUN_ID, "task")
    registry.execute(
        """
        INSERT INTO registry.run_participants
            (agent_id, run_id, parent_agent_id, crew_template, role)
        VALUES (%s, %s, %s, %s, %s)
        """,
        ("fd-implementer-01X", RUN_ID, "pm-1", "feature-delivery", "implementer"),
    )
    participants = RunParticipants(registry)
    assert participants.active("fd-implementer-01X")["run_id"] == RUN_ID

    registry.execute(
        "UPDATE registry.run_participants"
        " SET status = 'terminated', terminated_at = now()"
        " WHERE agent_id = %s",
        ("fd-implementer-01X",),
    )
    assert participants.active("fd-implementer-01X") is None


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
    companies = Companies(registry)
    companies.upsert(
        "hello-saas", {"namespace": "hello-saas", "infra": {"sandbox": {"cpu": "1"}}}
    )
    assert companies.get("hello-saas")["infra"]["sandbox"]["cpu"] == "1"
    assert companies.by_namespace("hello-saas")["namespace"] == "hello-saas"
    assert companies.get("nobody") is None
    assert companies.by_namespace("nowhere") is None


def test_the_pause_is_a_company_fact_that_survives_a_re_apply(registry: Registry):
    """The console pauses under a name, the heartbeat reads who and when;
    a blueprint re-apply leaves the pause alone; resume clears it; an
    unapplied company is simply not paused."""
    companies = Companies(registry)
    assert companies.paused("hello-saas") is None
    companies.upsert("hello-saas", {"namespace": "hello-saas"})
    assert companies.paused("hello-saas") is None
    companies.pause("hello-saas", "owner")
    paused = companies.paused("hello-saas")
    assert paused is not None and paused["by"] == "owner" and paused["at"]
    companies.upsert("hello-saas", {"namespace": "hello-saas", "vision": "v2"})
    assert companies.paused("hello-saas") == paused
    assert companies.get("hello-saas")["vision"] == "v2"
    companies.resume("hello-saas")
    assert companies.paused("hello-saas") is None


def test_budget_ledger_attributes_and_sums(registry: Registry):
    """One ledger with dimensions: every entry fully attributed,
    the per-run SUM is what the LLM gateway's refusal compares against the cap."""
    ledger = BudgetLedger(registry)
    Runs(registry).start(RUN_ID, "task")
    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="implementer-1",
        parent_agent_id="",
        role="implementer",
        model="mock-brain",
        prompt_tokens=100,
        completion_tokens=50,
        cached_tokens=50,
        reasoning_tokens=25,
        usd=0.0003,
    )
    ledger.record(
        company="hello-saas",
        run_id=RUN_ID,
        agent_id="fd-implementer-01X",
        parent_agent_id="pm-1",
        role="implementer",
        model="mock-brain",
        prompt_tokens=10,
        completion_tokens=5,
        cached_tokens=5,
        reasoning_tokens=2,
        usd=0.0001,
    )
    assert abs(ledger.spent_for_run(RUN_ID) - 0.0004) < 1e-9
    entries = ledger.entries_for_run(RUN_ID)
    assert [e["agent_id"] for e in entries] == ["implementer-1", "fd-implementer-01X"]
    assert entries[1]["parent_agent_id"] == "pm-1"
    assert (entries[0]["cached_tokens"], entries[0]["reasoning_tokens"]) == (50, 25)
    assert (entries[1]["cached_tokens"], entries[1]["reasoning_tokens"]) == (5, 2)
    assert ledger.spent_for_run("run-unmetered") == 0.0


def test_spawn_count_is_distinct_live_crews_per_conductor(registry: Registry):
    """The spawn count is distinct live {run, crew} pairs per conductor —
    what the compiled concurrency cap reads off the decision input."""
    Runs(registry).start(RUN_ID, "task")
    participants = RunParticipants(registry)
    participants.add("fd-impl-1", RUN_ID, "pm-1", "feature-delivery", "implementer")
    participants.add("fd-second-1", RUN_ID, "pm-1", "feature-delivery", "second")
    assert participants.active_spawn_count("pm-1") == 1  # one crew, two agents
    participants.add("fr-reviewer-1", RUN_ID, "pm-1", "feature-review", "reviewer")
    assert participants.active_spawn_count("pm-1") == 2
    participants.terminate_for_run(RUN_ID)
    assert participants.active_spawn_count("pm-1") == 0


def test_revocation_is_final_and_a_late_mint_cannot_revive_it(registry: Registry):
    """The only way to mint onto a terminated row is to be a
    retry that lost the race with its run's teardown. Reviving it would
    resurrect an identity the platform revoked — and leave an ephemeral pod
    holding it with nothing left to collect the pod. Revocation is final;
    the late caller is told to stop."""
    Runs(registry).start(RUN_ID, "task")
    participants = RunParticipants(registry)
    participants.add(
        "feature-delivery-implementer-z",
        RUN_ID,
        "pm-1",
        "feature-delivery",
        "implementer",
    )
    assert participants.terminate_for_run(RUN_ID) == ["feature-delivery-implementer-z"]

    with pytest.raises(ParticipantRevoked):
        participants.add(
            "feature-delivery-implementer-z",
            RUN_ID,
            "pm-1",
            "feature-delivery",
            "implementer",
        )
    # Still revoked: the failed mint changed nothing.
    assert participants.active("feature-delivery-implementer-z") is None
    rows = registry.query(
        "SELECT status FROM registry.run_participants WHERE agent_id = %s",
        ("feature-delivery-implementer-z",),
    )
    assert rows[0]["status"] == "terminated"


# -- the heartbeat's observations ----------------------------------


def test_active_count_counts_every_running_run_of_the_company(registry: Registry):
    """Scope, implement and review runs all occupy the company; another
    company's runs and finished runs do not."""
    runs = Runs(registry)
    runs.start("run-ac-1", "implement issue #1", "implement", None, "hello-saas")
    runs.start("run-ac-2", "review PR", "review", None, "hello-saas")
    runs.start("run-ac-3", "scope the next slice", "scope", None, "hello-saas")
    runs.start("run-ac-4", "implement issue #2", "implement", None, "other-co")
    runs.start("run-ac-5", "implement issue #9", "implement", None, "hello-saas")
    runs.finish("run-ac-5", "done")
    assert Runs(registry).active_count("hello-saas") == 3


def test_issue_attempts_fold_every_run_on_the_ticket(registry: Registry):
    """One `Attempts` per issue, from the canonical task: the count spans
    every status, `live` is any running run, and `concluded` is a delivered
    PR on any of them — a failed run that delivered still concluded, an
    aborted one did not. Free-form tasks and other companies are not
    keyed."""
    runs = Runs(registry)
    runs.start("run-in-1", "implement issue #3", "implement", None, "hello-saas")
    runs.start("run-in-2", "implement issue #12", "implement", None, "hello-saas")
    runs.finish("run-in-2", "failed", error="litellm.Timeout")  # aborted
    runs.start("run-in-3", "implement issue #12", "implement", None, "hello-saas")
    runs.finish("run-in-3", "failed", error="teardown: boom", pr_url="https://p/9")
    runs.start("run-in-4", "a free-form task file", "implement", None, "hello-saas")
    runs.start("run-in-5", "implement issue #7", "implement", None, "other-co")
    assert Runs(registry).issue_attempts("hello-saas") == {
        3: Attempts(count=1, live=True, concluded=False),
        12: Attempts(count=2, live=False, concluded=True),
    }


def test_a_refused_delivery_gives_its_ticket_back(registry: Registry):
    """A delivery the platform refused does not conclude its ticket: the
    attempt still counts, and the next attempt reads why, oldest first.
    Merged and superseded deliveries still conclude."""
    runs = Runs(registry)
    for run_id, number, pr in (
        ("run-rf-1", 5, 21),
        ("run-rf-2", 5, 22),
        ("run-rf-3", 6, 23),
        ("run-rf-4", 7, 24),
    ):
        runs.start(run_id, f"implement issue #{number}", "implement", None, "hello-saas")
        runs.finish(run_id, "done", pr_url=f"https://git.test/r/pull/{pr}")
    runs.mark_pr_settled("hello-saas", 21, "refused", "owner rejected the merge: no")
    runs.mark_pr_settled("hello-saas", 22, "refused", "nobody answered within 60 minutes")
    runs.mark_pr_settled("hello-saas", 23, "merged")
    runs.mark_pr_settled("hello-saas", 24, "superseded")
    assert Runs(registry).issue_attempts("hello-saas") == {
        5: Attempts(count=2, live=False, concluded=False),
        6: Attempts(count=1, live=False, concluded=True),
        7: Attempts(count=1, live=False, concluded=True),
    }
    assert runs.refusals("hello-saas", 5) == [
        ("https://git.test/r/pull/21", "owner rejected the merge: no"),
        ("https://git.test/r/pull/22", "nobody answered within 60 minutes"),
    ]
    assert runs.refusals("hello-saas", 6) == []
    assert runs.refusals("other-co", 5) == []
    assert runs.get("run-rf-3").pr_settled_reason is None


def test_spent_today_sums_the_company_since_utc_midnight(registry: Registry):
    ledger = BudgetLedger(registry)
    entry = dict(
        agent_id="fd-impl-1",
        parent_agent_id="pm-1",
        role="implementer",
        model="mock",
        prompt_tokens=1,
        completion_tokens=1,
        cached_tokens=0,
        reasoning_tokens=0,
    )
    ledger.record(company="hello-saas", run_id="run-sp-1", usd=2.5, **entry)
    ledger.record(company="hello-saas", run_id="run-sp-2", usd=1.5, **entry)
    ledger.record(company="other-co", run_id="run-sp-3", usd=9.0, **entry)
    # An entry from before today must not count against the daily cap.
    registry.execute(
        "UPDATE registry.budget_ledger SET ts = now() - interval '1 day'"
        " WHERE run_id = 'run-sp-2'"
    )
    assert BudgetLedger(registry).spent_today("hello-saas") == 2.5


def test_delivered_pr_urls_are_done_implement_runs_only(registry: Registry):
    runs = Runs(registry)
    runs.start("run-pr-1", "implement issue #1", "implement", None, "hello-saas")
    runs.finish("run-pr-1", "done", pr_url="https://git.test/r/pull/4")
    runs.start("run-pr-2", "implement issue #2", "implement", None, "hello-saas")
    runs.finish("run-pr-2", "failed", error="boom")  # no PR, not delivered
    runs.start("run-pr-3", "implement issue #3", "implement", None, "hello-saas")
    runs.finish("run-pr-3", "done", pr_url="https://git.test/r/pull/9")
    runs.start("run-pr-4", "implement issue #4", "implement", None, "other-co")
    runs.finish("run-pr-4", "done", pr_url="https://git.test/r/pull/8")
    assert Runs(registry).delivered_pr_urls("hello-saas") == [
        "https://git.test/r/pull/4",
        "https://git.test/r/pull/9",
    ]
    # The world settled one: remembered on the run, offered no more.
    runs.mark_pr_settled("hello-saas", 4, "refused", "closed outside the platform")
    assert Runs(registry).delivered_pr_urls("hello-saas") == ["https://git.test/r/pull/9"]
    assert runs.get("run-pr-1").pr_settled == "refused"
    assert runs.get("run-pr-1").pr_settled_reason == "closed outside the platform"


def test_review_attempts_conclude_on_a_recorded_verdict(registry: Registry):
    """The review's conclusion is the verdict on its row, not its status:
    a failed run whose reviewer judged (rework filed, gate rejected) stays
    concluded; a failed run with no verdict aborted."""
    runs = Runs(registry)
    runs.start("run-rv-1", "review pull request #4", "review", None, "hello-saas")
    runs.start("run-rv-2", "review pull request #9", "review", None, "hello-saas")
    runs.finish("run-rv-2", "failed", error="agent loop did not finish")
    runs.start("run-rv-3", "review pull request #11", "review", None, "hello-saas")
    runs.record_verdict("run-rv-3", "fail")
    runs.finish("run-rv-3", "failed", error="merge gate rejected")
    runs.start("run-rv-4", "review pull request #8", "review", None, "other-co")
    assert Runs(registry).review_attempts("hello-saas") == {
        4: Attempts(count=1, live=True, concluded=False),
        9: Attempts(count=1, live=False, concluded=False),
        11: Attempts(count=1, live=False, concluded=True),
    }
    assert Runs(registry).get("run-rv-3").verdict == "fail"
    assert Runs(registry).get("run-rv-3").as_dict()["verdict"] == "fail"
    assert Runs(registry).get("run-rv-2").verdict is None


def test_one_live_implement_run_per_issue(registry: Registry):
    """The partial unique index, end to end: a second live run for the
    same issue dies structurally; a retry of the same run lands on its own
    row; a finished run frees the slot; other companies are unaffected."""
    runs = Runs(registry)
    runs.start("run-dup-1", "implement issue #7", "implement", None, "hello-saas")
    with pytest.raises(RuntimeError, match="one live run per work item"):
        runs.start("run-dup-2", "implement issue #7", "implement", None, "hello-saas")
    runs.start("run-dup-1", "implement issue #7", "implement", None, "hello-saas")
    runs.finish("run-dup-1", "done")
    runs.start("run-dup-3", "implement issue #7", "implement", None, "hello-saas")
    runs.start("run-dup-4", "implement issue #7", "implement", None, "other-co")


def test_one_live_review_run_per_pr(registry: Registry):
    """The review twin of the issue index: a second live review of one PR
    dies structurally, a retry lands on its own row, a finished run frees
    the slot, and other companies are unaffected."""
    runs = Runs(registry)
    runs.start("run-rd-1", "review pull request #7", "review", None, "hello-saas")
    with pytest.raises(RuntimeError, match="one live run per work item"):
        runs.start("run-rd-2", "review pull request #7", "review", None, "hello-saas")
    runs.start("run-rd-1", "review pull request #7", "review", None, "hello-saas")
    runs.finish("run-rd-1", "failed", error="no verdict")
    runs.start("run-rd-3", "review pull request #7", "review", None, "hello-saas")
    runs.start("run-rd-4", "review pull request #7", "review", None, "other-co")


def test_free_form_tasks_are_not_constrained(registry: Registry):
    """Only the canonical issue-run task is keyed: two runs of the same
    task file are two deliberate human acts, not a race."""
    runs = Runs(registry)
    runs.start("run-ff-1", "the same task file text", "implement", None, "hello-saas")
    runs.start("run-ff-2", "the same task file text", "implement", None, "hello-saas")


def test_implement_run_for_pr_finds_the_lineage(registry: Registry):
    """The PR's run lineage, keyed the way the heartbeat observation keys
    PRs: the number is the URL's tail — and #4 must not match #14."""
    runs = Runs(registry)
    runs.start("run-ln-1", "implement issue #1", "implement", None, "hello-saas")
    runs.finish("run-ln-1", "done", pr_url="https://git.test/r/pull/14")
    runs.start("run-ln-2", "implement issue #2", "implement", None, "hello-saas")
    runs.finish("run-ln-2", "done", pr_url="https://git.test/r/pull/4")
    runs.start("run-ln-3", "implement issue #3", "implement", None, "other-co")
    runs.finish("run-ln-3", "done", pr_url="https://git.test/r/pull/4")
    found = Runs(registry).implement_run_for_pr("hello-saas", 4)
    assert found is not None and found.run_id == "run-ln-2"
    assert Runs(registry).implement_run_for_pr("hello-saas", 999) is None


def test_participants_for_run_includes_terminated_rows(registry: Registry):
    """The lineage outlives the identities: an implement run's
    ephemeral agents are revoked long before its PR is reviewed, and the
    independence check must still see them."""
    runs = Runs(registry)
    runs.start("run-lin-a", "implement issue #5", "implement", None, "hello-saas")
    participants = RunParticipants(registry)
    participants.add("lin-impl-1", "run-lin-a", "pm-1", "feature-delivery", "implementer")
    participants.terminate_for_run("run-lin-a")
    rows = participants.for_run("run-lin-a")
    assert [(r["agent_id"], r["status"]) for r in rows] == [("lin-impl-1", "terminated")]
    assert participants.for_run("run-nowhere") == []


# -- filed tickets ---------------------------------------------------------


def test_filed_tickets_are_remembered_within_the_lag_window(registry: Registry):
    """The gateway records each ticket once; the observation asks for the
    ones filed within the host's listing lag, by repository."""
    filed = FiledTickets(registry)
    filed.record("github.com/example/repo", 7, RUN_ID)
    filed.record("github.com/example/repo", 7, "run-other")  # a no-op
    filed.record("github.com/example/repo", 9, RUN_ID)
    filed.record("github.com/example/other", 3, RUN_ID)
    assert FiledTickets(registry).filed_since("github.com/example/repo", 60) == [7, 9]
    assert FiledTickets(registry).filed_since("github.com/example/repo", 0) == []
    # The console's read: every ticket on the repository, with who filed it.
    rows = FiledTickets(registry).filed("github.com/example/repo")
    assert [(r["number"], r["run_id"]) for r in rows] == [(7, RUN_ID), (9, RUN_ID)]
    assert all(r["filed_at"] is not None for r in rows)


def test_last_at_is_the_latest_matching_row(registry: Registry):
    """The console's read of when a run last parked: one row, the latest,
    never the story."""
    audit = AuditStore(registry)
    for ts, rule in (
        ("2026-01-01T00:00:00+00:00", "gate_parked"),
        ("2026-01-01T01:00:00+00:00", "gate_approved"),
        ("2026-01-01T02:00:00+00:00", "gate_parked"),
    ):
        audit.append(
            AuditEvent(
                ts=ts,
                run_id=RUN_ID,
                agent_id="orchestrator",
                role="platform",
                tool="gate.budget",
                args_digest="d",
                decision="deny",
                rule=rule,
                latency_ms=0,
                backend_status=None,
                source="process-gate",
            )
        )
    last = audit.last_at(RUN_ID, "gate.budget", "gate_parked")
    assert last.isoformat() == "2026-01-01T02:00:00+00:00"
    assert audit.last_at(RUN_ID, "gate.merge", "gate_parked") is None


def test_count_allowed_counts_answered_allows_only(registry: Registry):
    """A per-run cap reads the calls the gateway allowed and the host
    answered; a deny or a failed backend does not spend the cap."""
    store = AuditStore(registry)
    for decision, status in (("allow", "ok"), ("allow", "error"), ("deny", None)):
        store.append(
            event(tool="architecture.request", decision=decision, backend_status=status)
        )
    assert store.count_allowed(RUN_ID, "architecture.request") == 1
    assert store.count_allowed(RUN_ID, "git.open_pr") == 0


# -- the console's reads --------------------------------------------------------


def test_now_is_the_databases_clock(registry: Registry):
    stamp = registry.now()
    assert "T" in stamp and ("+" in stamp or stamp.endswith("Z"))


def test_runs_since_a_fence_are_the_companys_runs_after_it(registry: Registry):
    runs = Runs(registry)
    runs.start("run-before", "task a", company="hello-saas")
    fence = registry.now()
    runs.start("run-after", "task b", company="hello-saas")
    runs.start("run-elsewhere", "task c", company="other-co")
    runs.start("heartbeat-hello-saas", "tick", workflow="heartbeat", company="hello-saas")
    since = [r.run_id for r in runs.since("hello-saas", fence)]
    assert since == ["run-after"]
    everything = [r.run_id for r in runs.since("hello-saas", None)]
    assert everything == ["run-before", "run-after"]


def test_spent_since_a_fence_sums_the_companys_ledger_after_it(registry: Registry):
    ledger = BudgetLedger(registry)
    Runs(registry).start(RUN_ID, "task", company="hello-saas")

    def entry(usd: float, company: str = "hello-saas") -> None:
        ledger.record(
            company=company,
            run_id=RUN_ID,
            agent_id="implementer-1",
            parent_agent_id="",
            role="implementer",
            model="mock-brain",
            prompt_tokens=10,
            completion_tokens=5,
            cached_tokens=0,
            reasoning_tokens=0,
            usd=usd,
        )

    entry(0.5)
    fence = registry.now()
    entry(0.25)
    entry(1.0, company="other-co")
    assert ledger.spent_since("hello-saas", fence) == {
        "usd": 0.25,
        "prompt_tokens": 10,
        "completion_tokens": 5,
    }
    assert ledger.spent_since("hello-saas", None)["usd"] == 0.75


def test_story_since_a_fence(registry: Registry):
    audit = AuditStore(registry)
    audit.append(event(tool="before"))
    fence = registry.now()
    audit.append(event(tool="after"))
    assert [e["tool"] for e in audit.story(RUN_ID, fence)] == ["after"]
    assert [e["tool"] for e in audit.story(RUN_ID)] == ["before", "after"]


def test_latest_human_verdict_is_the_consoles_last_word(registry: Registry):
    """Only the console's rows are verdicts; the latest wins; another run's
    or another gate's verdict is never this one's."""
    audit = AuditStore(registry)

    def verdict(approved: bool, gate: str = "merge", run_id: str = RUN_ID, **extra):
        audit.append(
            event(
                run_id=run_id,
                agent_id="owner",
                role="human",
                lifecycle="human",
                tool=GATE_RESOLVE,
                rule=verdict_rule(gate, approved),
                source="console",
                **extra,
            )
        )

    assert audit.latest_human_verdict(RUN_ID, "merge") is None
    # The workflow's own process-gate row is not a verdict.
    audit.append(
        event(tool=gate_event(MERGE_GATE), rule="gate_approved", source="process-gate")
    )
    assert audit.latest_human_verdict(RUN_ID, "merge") is None
    verdict(True, ts="2026-01-01T00:00:01+00:00")
    verdict(True, run_id="run-other", ts="2026-01-01T00:00:05+00:00")
    verdict(False, gate="budget", ts="2026-01-01T00:00:06+00:00")
    read = audit.latest_human_verdict(RUN_ID, "merge")
    assert read["principal"] == "owner" and read["approved"] is True
    verdict(False, ts="2026-01-01T00:00:02+00:00", rationale="not this way")
    assert audit.latest_human_verdict(RUN_ID, "merge")["approved"] is False
    assert audit.latest_human_verdict(RUN_ID, "merge")["rationale"] == "not this way"
    assert audit.latest_human_verdict(RUN_ID, "budget")["approved"] is False
