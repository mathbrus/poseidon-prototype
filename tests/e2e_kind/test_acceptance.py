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
  2 durable       test_happy_path (kill-worker resume),
                  test_registry_durability, test_crew_fulfillment
                  (worker-kill crew sweep); HERE: the killed happy-path run itself
                  leaked no identity row, no ephemeral pod, no registration
  3 SDLC          test_scope + test_review_gate (the strategy scoped into
                  a ticket, an operator's `poseidon start implement <number>`, review
                  with parked gate, escalation, API approval, platform
                  merge); test_verify_gate (the platform's own test run, not
                  the implementer's claim, is the gate of record);
                  test_audit_story (one run_id reconstructs a run's whole story)
  4 governance    test_injection (injection vs compiled policy), test_governance,
                  test_injected_conductor, test_redteam (sandbox containment +
                  egress), test_containment (agents reach only the gateways;
                  the Temporal frontend is fenced from every platform pod but
                  the worker and the console), test_sandbox_k8s (the deadline
                  kills a runaway); HERE: all six audit sources are real in
                  this suite execution — nothing seeded
  5 budgets       test_budgets (refusal at the cap, API-visible park, process-gate
                  escalation, full {run, parent, agent, role} tuple)
  6 lean standing test_operator_lifecycle (exactly one AgentInstance),
                  test_scope, test_ephemeral_identity, test_ephemeral_pods,
                  test_workload_identity; HERE: the registry's standing agents
                  agrees, and a live minted implementer is denied outside its
                  compiled allow-list at the gateway
  7 cluster-      by construction, not by a module: `cluster.py` reaches
    neutral       the cluster through the kubectl context and .env alone,
                  and the kind-specific piece is deploy/kind/kind-up.sh.
  8 unattended    test_heartbeat_redteam (an attacker-filed issue consumed by
                  the heartbeat dies at the gates; a review that dies
                  without a verdict is retried once under max_attempts,
                  then abandoned on the record), test_injected_conductor
                  (a poisoned strategy cannot route work to a crew outside
                  may_spawn nor exfiltrate) and test_overnight (the whole
                  arc — scope, implement, review — with one human act: the
                  merge approval out; nothing in).
  9 merge policy  test_delegated (in-class passing work is scoped,
                  implemented, reviewed and merged with no human act at
                  all — the arc runs through the merge) and
                  test_split_oracle_redteam (a steered reviewer smuggling
                  a carve-out change is refused by the compiled decision,
                  not the model).
 10 split oracle  test_split_oracle_redteam (the implementer cannot PR the
                  contract tree, cannot launder a red baseline to green
                  through a committed collect-ignore, and a steered contract
                  author's gutted spec parks on a human with its deletions
                  counted) and test_delegated's contract arc (the PM files
                  contract-then-implement, the contract PR parks for one human
                  merge, then in-class work auto-merges on a green base-ref
                  contract).
 11 documents     test_documents (a vision-only repository bootstraps both
                  documents through the steward; an architecture request
                  becomes a stewardship ticket the loop implements; an
                  implementer cannot deliver into the masterplan and the
                  strategy is written once, for the steward too);
                  test_scope (the strategy becomes the company's own
                  ticket; scoping is refused by name at max_open_tickets).

 12 the console  test_console_identity (a human is known by the token the
                  cluster issued for the console's audience and refused at
                  the gateways; an agent is refused at the console; a human
                  the company did not name is refused by name; no platform
                  identity can mint a human's token) and
                  test_console_redteam (a merge signal delivered straight to
                  the workflow engine, past the console, releases nothing —
                  the run fails on `merge_resolve_no_human_verdict` with the
                  PR unmerged — while the same verdict through the console
                  merges and names the person); test_review_gate (the release
                  read the recorded verdict); HERE: the console's rows are
                  among the six sources.
 13 the pause    test_pause (`poseidon pause` holds the heartbeat and
                  nothing else: a paused tick on a startable backlog starts
                  nothing and names the person, a wake leaves it paused, a
                  stranger cannot lift it, a resume starts the held work,
                  and a run in flight outlives a pause).

The suite's own discipline is proved too: test_brain_guard (the session
refuses a paid brain without the explicit opt-in) and test_quiet (the exit
contract, ordered last: only the standing heartbeat running, asleep on an
empty backlog; no run non-terminal).

The conftest orders this module after every module whose runs it reads:
the five-source assertion reads what the whole suite really produced.
"""

import asyncio
import json
import subprocess

import yaml

from common.audit import RATIONALE_EVENTS, SOURCES
from common.ids import new_run_id
from schema.tools import RATIONALE_TOOLS

from .blueprints import (
    HELLO_SAAS,
    bundle_revision_from_cr,
    pristine_profile,
    suite_document,
)
from .cluster import (
    COMPANY,
    COMPANY_NS,
    audit_story,
    console_env,
    crew_activities,
    events_since,
    kubectl,
    mcp_call,
    projected_token,
    psql_json,
    registry,
    run_kubectl,
    sandbox_pods,
    worker_settings,
)
from .conftest import REPO_ROOT

# The decisions that owe a rationale: the actor's stated reason rides the
# row in cleartext (the glossary's *rationale*) — the platform's own events
# that commit the company, and the gateway tools that do.
COMMITTING_TOOLS = RATIONALE_EVENTS | RATIONALE_TOOLS

ALL_SOURCES = set(SOURCES)


# -- 1 declarative: compile → preview, scripted, against the live cluster ------


def _poseidon(env: dict, *args: str) -> subprocess.CompletedProcess:
    """`poseidon …` exactly as a person runs it: the real CLI, as the
    human `owner`, against the cluster's console."""
    return subprocess.run(
        ["uv", "run", "poseidon", *args],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
        env=console_env(env),
    )


def test_compile_and_preview_close_the_declarative_loop(stack, env, tmp_path):
    """Criterion 1's first two verbs, e2e: previewing the applied
    blueprint through the console reports *no changes* (the cluster is
    converged on exactly what the document says, and the console compiled
    against the live registry), an edit previews as exactly that one
    change, and a local compile produces byte-for-byte the bundle revision
    the cluster serves — compile is what's live."""
    # Preview of the applied document — the profile the suite leaves
    # applied: a converged cluster diffs empty.
    pristine = _poseidon(
        env, "blueprint", "preview", str(suite_document(pristine_profile()))
    )
    assert pristine.returncode == 0, pristine.stderr
    assert f"{COMPANY}: no changes" in pristine.stdout, pristine.stdout
    assert "budget:" in pristine.stdout

    # Preview of an edit: exactly the edit, nothing else.
    document = yaml.safe_load(HELLO_SAAS.read_text())
    assert "lead" not in document["standing"]
    document["standing"]["lead"] = {
        "role": "pm",
        "count": 1,
        "may_spawn": {"crew": ["feature-delivery"], "max_concurrent": 1},
    }
    edited = tmp_path / "hello-saas-second-hire.yaml"
    edited.write_text(yaml.safe_dump(document, sort_keys=False))
    preview = _poseidon(env, "blueprint", "preview", str(edited))
    assert preview.returncode == 0, preview.stderr
    assert f"{COMPANY}: 1 change(s)" in preview.stdout, preview.stdout
    assert "+ lead-1" in preview.stdout

    # Compile, scripted and cluster-free (`python -m compiler`, what the CI
    # policy job runs), emitting the bundle to disk.
    out = tmp_path / "compiled-bundle"
    compiled = subprocess.run(
        ["uv", "run", "python", "-m", "compiler", str(HELLO_SAAS), "--out", str(out)],
        capture_output=True,
        text=True,
        timeout=300,
        cwd=REPO_ROOT,
    )
    assert compiled.returncode == 0, compiled.stderr
    assert list(out.glob("*.rego")), "compile emitted no Rego files"
    plan_dict = json.loads(compiled.stdout)
    assert plan_dict["company"]["name"] == COMPANY

    # Compile-is-what's-live: the revision is a content digest (registry
    # `pack`), so equality here means the served policy is exactly what
    # this working tree's compiler emits from the document the suite
    # leaves applied — the pristine profile, the example on the suite's
    # repository and clock with review off the loop's own initiative.
    from compiler import compile_blueprint
    from registry import pack
    from schema import load_blueprint

    plan = compile_blueprint(load_blueprint(suite_document(pristine_profile())))
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
        "SELECT agent_id, standing_name, role, namespace"
        " FROM registry.agent_instances WHERE company = :'company') i",
        company=COMPANY,
    )
    assert [(r["agent_id"], r["role"], r["namespace"]) for r in rows] == [
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


def test_live_implementer_cannot_exceed_its_compiled_allowlist(
    stack, env: dict, tmp_path
) -> None:
    """Criterion 6's last clause, live: a *real bound* implementer — its own
    per-run ServiceAccount, a projected token, an identity row, full
    pipeline — is denied the tools outside its compiled allow-list at the
    gateway (deny by rule, audited), while its in-scope surface still
    answers. test_crew_fulfillment proves the same mid-run on a live
    implementer; this is the bound-identity proof in isolation."""
    from registry import Runs

    run_id = new_run_id()
    repo = env.get("TARGET_REPO", "github.com/example/repo")

    with registry() as db:
        crew = crew_activities(db, worker_settings(env, tmp_path))
        Runs(db).start(run_id, "implementer scope check", "implement", None, COMPANY)
        sa_name = None
        try:
            bound = asyncio.run(
                crew.bind_participant(
                    {
                        "run_id": run_id,
                        "company": COMPANY,
                        "crew_template": "feature-delivery",
                        "role": "implementer",
                        "parent_agent_id": "pm-1",
                    }
                )
            )
            sa_name = bound["service_account"]
            kubectl("-n", COMPANY_NS, "create", "serviceaccount", sa_name)
            token = projected_token(COMPANY_NS, sa_name)

            # Positive control: the implementer's in-scope surface answers
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
                    {
                        "run_id": run_id,
                        "repo": repo,
                        "title": "x",
                        "body": "y",
                        "rationale": "red-team: probing the allowlist",
                    },
                ),
                ("git.merge_pr", {"run_id": run_id, "repo": repo, "number": 1}),
                # The implementer never grades its own homework: closing a
                # ticket or a pull request is the platform's alone.
                (
                    "issues.close",
                    {
                        "run_id": run_id,
                        "repo": repo,
                        "number": 1,
                        "rationale": "red-team: closing my own ticket",
                    },
                ),
                (
                    "git.close_pr",
                    {
                        "run_id": run_id,
                        "repo": repo,
                        "number": 1,
                        "rationale": "red-team: closing a rival pull request",
                    },
                ),
            ):
                denied = mcp_call(env, token, tool, args)
                assert denied.isError, f"{tool} was not refused"
                assert "default_deny" in denied.content[0].text, (
                    f"{tool}: {denied.content[0].text}"
                )

            # Every deny is a first-class audit event under the bound
            # identity — gateway deny, not prompt hope, reconstructable.
            denies = {
                e["tool"]
                for e in audit_story(run_id)
                if e["source"] == "gateway"
                and e["decision"] == "deny"
                and e["agent_id"] == bound["agent_id"]
            }
            assert {
                "issues.create",
                "git.merge_pr",
                "issues.close",
                "git.close_pr",
            } <= denies, denies
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
    gateway tool decisions, llm-gateway metering/cap refusals, sandbox-infra
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

    # And the store stayed well-formed under everything the suite did: a
    # rule name on every row, and the actor's rationale on exactly the rows
    # that committed the company to something — on every one of those that
    # was allowed, and never on a refusal's row or a read's.
    for event in events:
        assert event["decision"] in ("allow", "deny"), event
        assert event["rule"], event
        if event["rationale"]:
            assert event["tool"] in COMMITTING_TOOLS, event
        if event["tool"] in COMMITTING_TOOLS and event["decision"] == "allow":
            assert event["rationale"], event
