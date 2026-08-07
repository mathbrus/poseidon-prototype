"""MVP acceptance: the exit criteria, scripted.

The acceptance suite is the whole of `tests/e2e_kind` (one command:
`scripts/acceptance.sh`, or `uv run pytest tests/e2e_kind -m e2e_kind`).
This module is its capstone: the clauses no earlier module asserts,
plus the cross-suite claims that only make sense once everything else ran.

Where each criterion is proven:

  1 declarative   test_operator_lifecycle (apply / edit / teardown with the
                  failed post-teardown call); HERE: compile and preview as
                  scripted e2e steps, and compile-is-what's-live (the local
                  compile's content-addressed revision == the served one)
  2 durable       test_happy_path (kill-worker resume), test_temporal,
                  test_registry_durability, test_crew_fulfillment
                  (worker-kill crew sweep); HERE: the killed E1 run itself
                  leaked no participant row, no crew pod, no registration
  3 SDLC          test_intake + test_review_gate (intake → implement →
                  review with parked gate, escalation, API approval,
                  platform merge)
  4 governance    test_injection (E2 on compiled policy), test_governance,
                  test_injected_conductor, test_egress, test_containment,
                  test_redteam; HERE: all five audit sources are real in
                  this suite execution — nothing seeded
  5 budgets       test_budgets (hard-stop, API-visible pause, process-gate
                  escalation, full {run, parent, participant, role} tuple)
  6 lean roster   test_operator_lifecycle (exactly one AgentInstance),
                  test_pm, test_crew_identity, test_crew_pods,
                  test_workload_identity; HERE: the registry's roster
                  agrees, and a live minted executor is denied outside its
                  compiled allow-list at the gateway
  7 parity        the suite is cluster-neutral (kubectl context + .env);
                  the kind-specific piece is deploy/kind/kind-up.sh alone.
                  The real-cluster half is deliberately out of scope for
                  now — kind only.

Named `test_zz_*` so pytest collects it last: the five-source assertion
reads what the whole suite really produced.
"""

import json
import os
import subprocess
from typing import Any

import yaml

from common.ids import new_run_id

from .conftest import (
    COMPANY_NS,
    PLATFORM_NS,
    POSTGRES_USER,
    REPO_ROOT,
    _psql_json,
    audit_story,
    sandbox_pods,
)
from .test_crew_identity import _cluster_secret, _registry
from .test_governance import _call
from .test_operator_lifecycle import (
    HELLO_SAAS,
    bundle_revision_from_cr,
    kubectl,
    port_forward,
)

PG_LOCAL_PORT = 15436  # distinct per module: forwards must never collide

ALL_SOURCES = {
    "gateway", "llm-gateway", "sandbox-infra", "process-gate",
    "process-lifecycle",
}


# -- criterion 1: compile → preview, scripted, against the live cluster ------


def _blueprint_cli(args: list[str], dsn: str) -> subprocess.CompletedProcess:
    """`poseidon blueprint …` exactly as an operator runs it, pointed at
    the cluster's registry."""
    return subprocess.run(
        ["uv", "run", "poseidon", "blueprint", *args],
        capture_output=True, text=True, timeout=300, cwd=REPO_ROOT,
        env={**os.environ, "REGISTRY_DSN": dsn},
    )


def test_compile_and_preview_close_the_declarative_loop(stack, tmp_path):
    """Criterion 1's first two verbs, e2e: previewing the applied
    blueprint against the live registry reports *no changes* (the cluster
    is converged on exactly what the document says), an edit previews as
    exactly that one change, and a local compile produces byte-for-byte
    the bundle revision the cluster serves — compile is what's live."""
    password = _cluster_secret("postgres-credentials", "POSTGRES_PASSWORD")
    with port_forward("postgres", PG_LOCAL_PORT, 5432):
        dsn = (
            f"postgresql://{POSTGRES_USER}:{password}"
            f"@127.0.0.1:{PG_LOCAL_PORT}/{POSTGRES_USER}"
        )

        # Preview of the applied document: a converged cluster diffs empty.
        pristine = _blueprint_cli(["preview", str(HELLO_SAAS)], dsn)
        assert pristine.returncode == 0, pristine.stderr
        assert "registry unreachable" not in pristine.stderr, (
            "preview fell back to diffing against nothing — the assertion"
            f" below would be vacuous: {pristine.stderr}"
        )
        assert "hello-saas: no changes" in pristine.stdout, pristine.stdout
        assert "budget:" in pristine.stdout

        # Preview of an edit: exactly the edit, nothing else.
        document = yaml.safe_load(HELLO_SAAS.read_text())
        assert document["departments"]["roles"]["pm"]["model_tier"] != "small"
        document["departments"]["roles"]["pm"]["model_tier"] = "small"
        edited = tmp_path / "hello-saas-small-pm.yaml"
        edited.write_text(yaml.safe_dump(document, sort_keys=False))
        preview = _blueprint_cli(["preview", str(edited)], dsn)
        assert preview.returncode == 0, preview.stderr
        assert "hello-saas: 1 change(s)" in preview.stdout, preview.stdout
        assert "~ pm-1" in preview.stdout
        assert "model_tier" in preview.stdout and "-> small" in preview.stdout

    # Compile, scripted (the CLI, not just the library), emitting the
    # bundle to disk like the CI policy job does.
    out = tmp_path / "compiled-bundle"
    compiled = _blueprint_cli(
        ["compile", str(HELLO_SAAS), "--no-live", "--out", str(out)], dsn=""
    )
    assert compiled.returncode == 0, compiled.stderr
    assert list(out.glob("*.rego")), "compile emitted no Rego files"
    plan_dict = json.loads(compiled.stdout)
    assert plan_dict["company"]["name"] == "hello-saas"

    # Compile-is-what's-live: the revision is a content digest (registry
    # `pack`), so equality here means the served policy is exactly what
    # this working tree's compiler emits from this blueprint.
    from compiler import compile_blueprint
    from registry.bundles import pack
    from schema import load_blueprint

    plan = compile_blueprint(load_blueprint(HELLO_SAAS))
    _, revision = pack(dict(plan.bundle.files))
    assert revision == bundle_revision_from_cr(), (
        "the cluster serves a bundle this blueprint + compiler do not produce"
    )


# -- criterion 2: the killed run's own leak check ----------------------------


def test_the_killed_run_left_nothing_behind(implement_run: dict) -> None:
    """E1 kills the worker mid-run; earlier modules assert the *sandbox*
    leaked nothing and prove the crew sweep on a separate run. Here the
    clause is closed on the killed run itself: its participant rows
    are terminated, its crew pod is gone, its sandbox registration is
    terminated — no leaked sandbox, identity, or participant row."""
    run_id = implement_run["run_id"]

    participants = _psql_json(
        "SELECT COALESCE(json_agg(p), '[]'::json) FROM ("
        "SELECT agent_id, status, terminated_at"
        " FROM registry.run_participants WHERE run_id = :'run_id') p",
        run_id, default="[]",
    )
    assert participants, "the crewed run minted no participants at all"
    for row in participants:
        assert row["status"] == "terminated", f"leaked participant: {row}"
        assert row["terminated_at"], f"terminated without a timestamp: {row}"

    # No live pod of any kind still carries the run's label (sandbox Jobs
    # and crew pods both do).
    assert sandbox_pods(run_id) == [], "a labelled pod outlived the run"

    registrations = _psql_json(
        "SELECT COALESCE(json_agg(r), '[]'::json) FROM ("
        "SELECT run_id, terminated_at FROM registry.sandbox_registrations"
        " WHERE run_id = :'run_id') r",
        run_id, default="[]",
    )
    assert registrations, "the run never registered a sandbox"
    for row in registrations:
        assert row["terminated_at"], f"leaked sandbox registration: {row}"


# -- criterion 6: the roster, from the registry's point of view --------------


def test_the_registry_roster_is_the_lean_blueprint(stack) -> None:
    """`exactly one standing AgentInstance` is asserted against the CRs in
    test_operator_lifecycle; the registry — which is what gateway authn
    enriches from — must agree, and must still agree at the end of the
    suite, after every module's teardown/re-apply cycles."""
    rows = _psql_json(
        "SELECT COALESCE(json_agg(i ORDER BY i.agent_id), '[]'::json) FROM ("
        "SELECT agent_id, hire, role_template, namespace"
        " FROM registry.agent_instances WHERE company = 'hello-saas') i",
        run_id="", default="[]",
    )
    assert [
        (r["agent_id"], r["role_template"], r["namespace"]) for r in rows
    ] == [("pm-1", "pm", COMPANY_NS)], (
        f"the registry roster is not the lean blueprint: {rows}"
    )

    # And the CRs still agree at suite end (drift check after all the
    # module finalizers): the PM is the only standing instance.
    instances = kubectl(
        "-n", COMPANY_NS, "get", "agentinstances",
        "-o", "jsonpath={.items[*].metadata.name}",
    ).stdout.split()
    assert instances == ["pm"], instances


def test_live_executor_cannot_exceed_its_compiled_allowlist(
    stack, env: dict, tmp_path
) -> None:
    """Criterion 6's last clause, live: a *real minted* executor —
    token, participants row, full pipeline — is denied the tools outside
    its compiled allow-list at the gateway (deny by rule, audited), while
    its in-scope surface still answers. The planner half of this claim is
    proven mid-run in test_crew_fulfillment; this is the executor's."""
    import asyncio

    from orchestrator.activities.crew import CrewActivities
    from orchestrator.config import WorkerSettings
    from registry import AuditStore, RunParticipants, Runs

    run_id = new_run_id()
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    token_file = tmp_path / "unused-token"
    token_file.write_text("unused")
    repo = env.get("TARGET_REPO", "github.com/example/repo")

    with _registry() as registry:
        settings = WorkerSettings(
            gateway_token_file=str(token_file),
            mcp_gateway_url=gateway_url,
            target_repo=repo,
            platform_jwt_key=_cluster_secret("platform-jwt-key", "PLATFORM_JWT_KEY"),
            ephemeral_ttl_s=900,
        )
        crew = CrewActivities(
            settings, RunParticipants(registry), AuditStore(registry)
        )
        Runs(registry).start(
            run_id, "step 25 executor scope check", "implement", None, "hello-saas"
        )
        try:
            minted = asyncio.run(crew.mint_participant({
                "run_id": run_id,
                "company": "hello-saas",
                "crew_template": "feature-delivery",
                "member": "executor",
                "role_template": "backend-engineer",
                "parent_agent_id": "pm-1",
            }))
            token = minted["token"]

            # Positive control: the executor's in-scope surface answers
            # (policy allow; the backend errors because no sandbox exists —
            # a denied *identity* would never reach the backend).
            control = _call(env, token, "sandbox.list_dir",
                            {"run_id": run_id, "path": "."})
            assert control.isError
            assert "denied by policy" not in control.content[0].text
            assert "no sandbox registered" in control.content[0].text

            # Out of scope: PM-only and platform-only tools, denied by rule.
            for tool, args in (
                ("issues.create", {"run_id": run_id, "repo": repo,
                                   "title": "x", "body": "y"}),
                ("git.merge_pr", {"run_id": run_id, "repo": repo, "number": 1}),
            ):
                denied = _call(env, token, tool, args)
                assert denied.isError, f"{tool} was not refused"
                assert "default_deny" in denied.content[0].text, (
                    f"{tool}: {denied.content[0].text}"
                )

            # Both denies are first-class audit events under the minted
            # identity — gateway deny, not prompt hope, reconstructable.
            denies = {
                e["tool"] for e in audit_story(run_id)
                if e["source"] == "gateway" and e["decision"] == "deny"
                and e["agent_id"] == minted["agent_id"]
            }
            assert {"issues.create", "git.merge_pr"} <= denies, denies
        finally:
            asyncio.run(crew.revoke_participants(run_id))
            registry.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,))
            registry.execute(
                "DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


# -- criterion 4: every audit source, real, in this suite execution ---------


def _events_since(fence: str) -> list[dict[str, Any]]:
    proc = subprocess.run(
        ["kubectl", "-n", PLATFORM_NS, "exec", "-i", "statefulset/postgres", "--",
         "psql", "-U", POSTGRES_USER, "-d", POSTGRES_USER, "-At",
         "-v", f"fence={fence}", "-f", "-"],
        input=(
            "SELECT COALESCE(json_agg(e), '[]'::json) FROM ("
            "SELECT source, run_id, decision, rule, tool"
            " FROM registry.audit_events WHERE ts >= :'fence') e"
        ),
        capture_output=True, text=True, timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    return json.loads(proc.stdout.strip() or "[]")


def test_all_five_audit_sources_are_real_in_this_suite(
    stack, db_session_start: str
) -> None:
    """Criterion 4's audit clause with no seeding anywhere: within this
    suite execution (fenced by the database's own clock at session start),
    real producers wrote every source in the closed audit-source vocabulary —
    gateway tool decisions, llm-gateway metering/hard-stops, sandbox-infra
    egress denies, process-gate escalations/verdicts, process-lifecycle
    mint/revoke — and each source correlates to runs by run_id.

    Runs alone against a fresh cluster this fails by design: it reads what
    the whole suite produced. Run the full acceptance suite."""
    events = _events_since(db_session_start)
    assert events, "no audit events in this session — run the full suite"

    sources = {e["source"] for e in events}
    assert sources == ALL_SOURCES, (
        f"missing sources: {sorted(ALL_SOURCES - sources)};"
        f" unknown sources: {sorted(sources - ALL_SOURCES)}"
    )

    # Every source produced run_id-correlated events (authn rejects may
    # legitimately carry none — the credential mapped to nothing).
    for source in sorted(ALL_SOURCES):
        assert any(
            e["run_id"] for e in events if e["source"] == source
        ), f"source {source} never correlated to a run"

    # And the store stayed well-formed under everything the suite did.
    for event in events:
        assert event["decision"] in ("allow", "deny"), event
        assert event["rule"], event
