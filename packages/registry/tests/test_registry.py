"""What the registry must guarantee for the services that depend on it."""

from datetime import datetime, timezone

from common.audit import AuditEvent
from registry.audit import AuditStore
from registry.db import Registry
from registry.runs import Runs
from registry.sandboxes import SandboxRegistrations

RUN_ID = "run-01REGISTRYTEST"


def event(**overrides) -> AuditEvent:
    fields = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "run_id": RUN_ID,
        "agent_id": "backend-engineer-1",
        "role": "backend-engineer",
        "tool": "sandbox.exec",
        "args_digest": "a" * 64,
        "decision": "allow",
        "rule": "sandbox_assigned_run",
        "latency_ms": 3,
        "backend_status": "ok",
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


def test_story_spans_sources(registry: Registry):
    """E4 at M1 scale: the gateway's decisions and the egress proxy's denies
    answer one query, which is the whole reason the JSONL files retired."""
    audit = AuditStore(registry)
    audit.append(event(tool="sandbox.exec", ts="2026-01-01T00:00:01+00:00"))
    audit.append(
        event(
            agent_id="egress-proxy",
            role="sandbox-infra",
            tool="sandbox.egress",
            decision="deny",
            rule="egress_not_allowlisted",
            backend_status=None,
            ts="2026-01-01T00:00:02+00:00",
        )
    )
    roles = [e["role"] for e in audit.story(RUN_ID)]
    assert roles == ["backend-engineer", "sandbox-infra"]


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


# -- the operator-owned tables (plan step 10) -------------------------------


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
