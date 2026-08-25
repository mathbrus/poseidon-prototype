"""The acceptance suite's index and capstone.

The acceptance suite is the whole of `tests/e2e_kind` (one command:
`scripts/acceptance.sh`, or `uv run pytest tests/e2e_kind -m e2e_kind`).
This module is its capstone: the clauses no earlier module asserts,
plus the cross-suite claims that only make sense once everything else ran.

What the suite proves, and where:

  1 declarative   test_operator_lifecycle (apply / edit / teardown with the
                  failed post-teardown call); HERE: compile and preview as
                  scripted e2e steps, and compile-is-what's-live (the local
                  compile's content-addressed revision == the served one)
  2 durable       test_happy_path (kill-worker resume), test_temporal,
                  test_registry_durability, test_crew_fulfillment
                  (worker-kill crew sweep); HERE: the killed happy-path run itself
                  leaked no participant row, no ephemeral pod, no registration
  3 SDLC          test_intake + test_review_gate (intake → implement →
                  review with parked gate, escalation, API approval,
                  platform merge)
  4 governance    test_injection (injection vs compiled policy), test_governance,
                  test_injected_conductor, test_egress, test_containment,
                  test_redteam; HERE: all five audit sources are real in
                  this suite execution — nothing seeded
  5 budgets       test_budgets (hard-stop, API-visible pause, process-gate
                  escalation, full {run, parent, participant, role} tuple)
  6 lean standing test_operator_lifecycle (exactly one AgentInstance),
                  test_pm, test_ephemeral_identity, test_ephemeral_pods,
                  test_workload_identity; HERE: the registry's standing agents
                  agrees, and a live minted executor is denied outside its
                  compiled allow-list at the gateway
  7 cluster-      the suite reaches the cluster through the kubectl
    neutral       context and .env alone; the kind-specific piece is
                  deploy/kind/kind-up.sh.
  8 unattended    test_conduct_redteam (an attacker-filed issue consumed by
                  the heartbeat dies at the gates) and test_overnight
                  (the whole arc with two human acts: one goal in, one
                  merge approval out).
  9 merge policy  test_delegated (in-class passing work merges with zero
                  human commands — the arc runs through the merge) and
                  test_merge_gate_redteam (a steered reviewer smuggling a
                  carve-out change is refused by the compiled decision, not
                  the model).
 10 split oracle  test_split_oracle_redteam (the implementer cannot PR the
                  contract tree, cannot launder a red baseline to green two
                  ways, and a steered contract author's gutted spec parks on
                  a human with its deletions counted) and test_delegated's
                  contract arc (the PM files contract-then-implement, the
                  contract PR parks for one human merge, then in-class work
                  auto-merges on a green base-ref contract).

The conftest orders this module after every module whose runs it reads:
the five-source assertion reads what the whole suite really produced.
"""

import json
import os
import subprocess

import yaml

from common.ids import new_run_id

from .blueprints import HELLO_SAAS, bundle_revision_from_cr
from .cluster import (
    COMPANY_NS,
    POSTGRES_USER,
    agent_image,
    audit_story,
    cluster_secret,
    events_since,
    kubectl,
    mcp_call,
    port_forward,
    projected_token,
    psql_json,
    registry,
    run_kubectl,
    sandbox_pods,
)
from .conftest import REPO_ROOT

ALL_SOURCES = {
    "gateway",
    "llm-gateway",
    "sandbox-infra",
    "process-gate",
    "process-lifecycle",
}


# -- 1 declarative: compile → preview, scripted, against the live cluster ------


def _blueprint_cli(args: list[str], dsn: str) -> subprocess.CompletedProcess:
    """`poseidon blueprint …` exactly as an operator runs it, pointed at
    the cluster's registry."""
    return subprocess.run(
        ["uv", "run", "poseidon", "blueprint", *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
        env={**os.environ, "REGISTRY_DSN": dsn},
    )


def test_compile_and_preview_close_the_declarative_loop(stack, tmp_path):
    """Criterion 1's first two verbs, e2e: previewing the applied
    blueprint against the live registry reports *no changes* (the cluster
    is converged on exactly what the document says), an edit previews as
    exactly that one change, and a local compile produces byte-for-byte
    the bundle revision the cluster serves — compile is what's live."""
    password = cluster_secret("postgres-credentials", "POSTGRES_PASSWORD")
    with port_forward("svc/postgres", 5432) as port:
        dsn = f"postgresql://{POSTGRES_USER}:{password}@127.0.0.1:{port}/{POSTGRES_USER}"

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
        assert document["standing"]["pm"]["model_tier"] != "small"
        document["standing"]["pm"]["model_tier"] = "small"
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
    from registry import pack
    from schema import load_blueprint

    plan = compile_blueprint(load_blueprint(HELLO_SAAS))
    _, revision = pack(dict(plan.bundle.files))
    assert revision == bundle_revision_from_cr(), (
        "the cluster serves a bundle this blueprint + compiler do not produce"
    )


# -- 2 durable: the killed run's own leak check ------------------------------


def test_the_killed_run_left_nothing_behind(implement_run: dict) -> None:
    """The happy-path fixture kills the worker mid-run; earlier modules
    assert the *sandbox*
    leaked nothing and prove the crew sweep on a separate run. Here the
    clause is closed on the killed run itself: its participant rows
    are terminated, its ephemeral pod is gone, its sandbox registration is
    terminated — no leaked sandbox, identity, or participant row."""
    run_id = implement_run["run_id"]

    participants = psql_json(
        "SELECT COALESCE(json_agg(p), '[]'::json) FROM ("
        "SELECT agent_id, status, terminated_at"
        " FROM registry.run_participants WHERE run_id = :'run_id') p",
        run_id=run_id,
    )
    assert participants, "the crewed run minted no participants at all"
    for row in participants:
        assert row["status"] == "terminated", f"leaked participant: {row}"
        assert row["terminated_at"], f"terminated without a timestamp: {row}"

    # No live pod of any kind still carries the run's label (sandbox Jobs
    # and ephemeral pods both do).
    assert sandbox_pods(run_id) == [], "a labelled pod outlived the run"

    registrations = psql_json(
        "SELECT COALESCE(json_agg(r), '[]'::json) FROM ("
        "SELECT run_id, terminated_at FROM registry.sandbox_registrations"
        " WHERE run_id = :'run_id') r",
        run_id=run_id,
    )
    assert registrations, "the run never registered a sandbox"
    for row in registrations:
        assert row["terminated_at"], f"leaked sandbox registration: {row}"


# -- 6 lean standing: the standing agents, from the registry's point of view --------------


def test_the_registry_roster_is_the_lean_blueprint(stack) -> None:
    """`exactly one standing AgentInstance` is asserted against the CRs in
    test_operator_lifecycle; the registry — which is what gateway authn
    enriches from — must agree, and must still agree at the end of the
    suite, after every module's teardown/re-apply cycles."""
    rows = psql_json(
        "SELECT COALESCE(json_agg(i ORDER BY i.agent_id), '[]'::json) FROM ("
        "SELECT agent_id, standing_role, role_template, namespace"
        " FROM registry.agent_instances WHERE company = 'hello-saas') i"
    )
    assert [(r["agent_id"], r["role_template"], r["namespace"]) for r in rows] == [
        ("pm-1", "pm", COMPANY_NS)
    ], f"the registry's standing agents are not the lean blueprint's: {rows}"

    # And the CRs still agree at suite end (drift check after all the
    # module finalizers): the PM is the only standing instance.
    instances = kubectl(
        "-n",
        COMPANY_NS,
        "get",
        "agentinstances",
        "-o",
        "jsonpath={.items[*].metadata.name}",
    ).split()
    assert instances == ["pm"], instances


def test_live_executor_cannot_exceed_its_compiled_allowlist(
    stack, env: dict, tmp_path
) -> None:
    """Criterion 6's last clause, live: a *real bound* executor — its own
    per-run ServiceAccount, a projected token, a participants row, full
    pipeline — is denied the tools outside its compiled allow-list at the
    gateway (deny by rule, audited), while its in-scope surface still
    answers. The planner half of this claim is proven mid-run in
    test_crew_fulfillment; this is the executor's."""
    import asyncio

    from orchestrator.activities.crew import CrewActivities
    from orchestrator.config import WorkerSettings
    from registry import (
        AuditStore,
        BudgetLedger,
        Companies,
        CrewRequests,
        RunParticipants,
        Runs,
    )

    run_id = new_run_id()
    gateway_url = env.get("MCP_GATEWAY_URL", "http://127.0.0.1:8080")
    token_file = tmp_path / "unused-token"
    token_file.write_text("unused")
    repo = env.get("TARGET_REPO", "github.com/example/repo")

    with registry() as db:
        settings = WorkerSettings(
            gateway_token_file=str(token_file),
            mcp_gateway_url=gateway_url,
            target_repo=repo,
            agent_image=agent_image(),
        )
        crew = CrewActivities(
            settings,
            RunParticipants(db),
            AuditStore(db),
            CrewRequests(db),
            Companies(db),
            BudgetLedger(db),
            Runs(db),
        )
        Runs(db).start(run_id, "executor scope check", "implement", None, "hello-saas")
        sa_name = None
        try:
            bound = asyncio.run(
                crew.bind_participant(
                    {
                        "run_id": run_id,
                        "company": "hello-saas",
                        "crew_template": "feature-delivery",
                        "crew_role": "executor",
                        "role_template": "backend-engineer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            sa_name = bound["service_account"]
            kubectl("-n", COMPANY_NS, "create", "serviceaccount", sa_name)
            token = projected_token(COMPANY_NS, sa_name)

            # Positive control: the executor's in-scope surface answers
            # (policy allow; the backend errors because no sandbox exists —
            # a denied *identity* would never reach the backend).
            control = mcp_call(
                env, token, "sandbox.list_dir", {"run_id": run_id, "path": "."}
            )
            assert control.isError
            assert "denied by policy" not in control.content[0].text
            assert "no sandbox registered" in control.content[0].text

            # Out of scope: PM-only and platform-only tools, denied by rule.
            for tool, args in (
                (
                    "issues.create",
                    {"run_id": run_id, "repo": repo, "title": "x", "body": "y"},
                ),
                ("git.merge_pr", {"run_id": run_id, "repo": repo, "number": 1}),
            ):
                denied = mcp_call(env, token, tool, args)
                assert denied.isError, f"{tool} was not refused"
                assert "default_deny" in denied.content[0].text, (
                    f"{tool}: {denied.content[0].text}"
                )

            # Both denies are first-class audit events under the bound
            # identity — gateway deny, not prompt hope, reconstructable.
            denies = {
                e["tool"]
                for e in audit_story(run_id)
                if e["source"] == "gateway"
                and e["decision"] == "deny"
                and e["agent_id"] == bound["agent_id"]
            }
            assert {"issues.create", "git.merge_pr"} <= denies, denies
        finally:
            asyncio.run(crew.revoke_participants(run_id))
            if sa_name:
                run_kubectl("-n", COMPANY_NS, "delete", "serviceaccount", sa_name)
            db.execute(
                "DELETE FROM registry.run_participants WHERE run_id = %s", (run_id,)
            )
            db.execute("DELETE FROM registry.runs WHERE run_id = %s", (run_id,))


# -- 4 governance: every audit source, real, in this suite execution ---------


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
    events = events_since(db_session_start)
    assert events, "no audit events in this session — run the full suite"

    sources = {e["source"] for e in events}
    assert sources == ALL_SOURCES, (
        f"missing sources: {sorted(ALL_SOURCES - sources)};"
        f" unknown sources: {sorted(sources - ALL_SOURCES)}"
    )

    # Every source produced run_id-correlated events (authn rejects may
    # legitimately carry none — the credential mapped to nothing).
    for source in sorted(ALL_SOURCES):
        assert any(e["run_id"] for e in events if e["source"] == source), (
            f"source {source} never correlated to a run"
        )

    # And the store stayed well-formed under everything the suite did.
    for event in events:
        assert event["decision"] in ("allow", "deny"), event
        assert event["rule"], event
