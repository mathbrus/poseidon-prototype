"""The acceptance suite's index and capstone.

The acceptance suite is the whole of `tests/e2e_kind` (one command:
`scripts/acceptance.sh`, or `uv run pytest tests/e2e_kind -m e2e_kind`).
This module is its capstone: the clauses no earlier module asserts,
plus the cross-suite claims that only make sense once everything else ran.

What the suite proves, and where:

  1 declarative   test_reconciler_lifecycle (apply / edit), test_delete
                  (teardown with the failed post-teardown call, no row and
                  no workflow history of the company left, the repository
                  purged, a company applied again with no history);
                  HERE: compile and preview as scripted e2e steps, and
                  compile-is-what's-live (the local compile's
                  content-addressed revision == the served one)
  2 durable       test_happy_path (kill-worker resume with the crew up,
                  and the crew swept by the replayed compensation),
                  test_registry_durability; HERE: the killed happy-path
                  run's sandbox registration is terminated
  3 SDLC          test_scope + test_review_gate (the strategy scoped into
                  a ticket, implemented, reviewed with a parked gate, a
                  verdict through the console, platform merge — every run
                  the heartbeat's); test_verify_gate (the platform's own
                  test run, not the implementer's claim, is the gate of
                  record); test_audit_story (one run_id reconstructs a
                  run's whole story)
  4 governance    test_injection (injection vs compiled policy), test_governance,
                  test_injected_conductor, test_redteam (sandbox containment +
                  egress, the deadline on every Job), test_containment
                  (agents reach only the gateways; the Temporal frontend is
                  fenced from every platform pod but the worker and the
                  console); HERE: all six audit sources are real in this
                  suite execution — nothing seeded
  5 budgets       test_budgets (refusal at the cap, API-visible park, the
                  person's verdict either way, full {run, parent, agent,
                  role} tuple)
  6 one standing  test_reconciler_lifecycle (exactly one AgentInstance),
                  test_scope, test_ephemeral_identity (a live minted
                  implementer is denied outside its compiled allow-list),
                  test_ephemeral_pods, test_workload_identity; HERE: the
                  registry's standing agents agree
  7 cluster-      by construction, not by a module: `cluster.py` reaches
    neutral       the cluster through the kubectl context and .env alone,
                  and the kind-specific piece is deploy/kind/kind-up.sh.
  8 unattended    test_heartbeat_redteam (an issue filed on the repository
                  is consumed by the heartbeat within its cap; a PR merged
                  by hand is never reviewed and closes its ticket; a review
                  that dies without a verdict spends the last attempt,
                  refuses its PR, and the ticket closes as not planned),
                  test_injected_conductor (a poisoned strategy cannot route
                  work to a crew outside may_spawn nor exfiltrate) and
                  test_review_gate (the whole arc — scope, implement,
                  review — with one human act: the merge verdict; nothing
                  in).
  9 automation    test_automated (the contract-first arc: passing work on
                  the automated paths is scoped, implemented, reviewed and
                  merged with no person — the one human act is the
                  contract's verdict) and test_split_oracle_redteam (a
                  steered reviewer smuggling a carve-out change is refused
                  by the compiled decision, not the model).
 10 contract tree test_split_oracle_redteam (the implementer cannot PR the
                  contract tree, cannot launder a red baseline to green
                  through a committed collect-ignore, and a steered contract
                  author's gutted spec parks on a human with its deletions
                  counted) and test_automated (the PM files
                  contract-then-implement, the contract PR parks for one
                  human verdict, then automated work merges on a green
                  base-ref contract).
 11 documents     test_documents (a vision-only repository bootstraps both
                  documents through the steward; an architecture request
                  becomes a stewardship ticket the platform shapes; the
                  strategy is written once, for the steward too);
                  test_scope (the strategy becomes the company's own
                  ticket; scoping is refused by name at max_open_tickets).

 12 the console  test_console_identity (a human is known by the token the
                  cluster issued for the console's audience and refused at
                  the gateways; an agent is refused at the console; a human
                  the company did not name is refused by name; no platform
                  identity can mint a human's token), test_console_redteam
                  (a merge signal delivered straight to the workflow
                  engine, past the console, releases nothing — the run
                  fails on `merge_resolve_no_human_verdict` and the PR is
                  refused, never merged) and test_review_gate (the same
                  verdict through the console merges, names the person,
                  and the release read the recorded verdict); HERE: the
                  console's rows are among the six sources.
 13 the pause    test_pause (`poseidon pause` holds the heartbeat and
                  nothing else: a paused tick on a startable backlog starts
                  nothing and names the person, a wake leaves it paused, a
                  stranger cannot lift it, a resume starts the held work,
                  and a run in flight outlives a pause).
 14 work endings  test_review_gate (a parked review holds no crew, no
                  sandbox and no place, so a one-place company implements
                  the next ticket while it waits; a person's rejection
                  refuses the delivery with their words on it and the
                  ticket comes back to be implemented again; a merge
                  conflicting with a moved base is refused and its ticket
                  comes back; a moved head is refused); test_scope (the PM
                  dismisses a planted ticket as not planned, and a
                  bootstrap ticket is refused by name);
                  test_heartbeat_redteam (an abandoned ticket closes as not
                  planned); test_split_oracle_redteam (an unanswered park
                  escalates at half its deadline and expires).

The suite's own discipline is proved too: test_brain_guard (the session
read the brain the cluster deploys; the refusal rule itself is pinned in
tests/test_brain_guard.py, without a cluster) and test_quiet (the exit
contract, ordered last: only the heartbeat running, asleep on an empty
backlog; no run non-terminal).

The conftest orders this module after every module whose runs it reads:
the six-source assertion reads what the whole suite really produced.
"""

import json
import subprocess

import yaml

from blueprint.tools import RATIONALE_TOOLS
from common.audit import RATIONALE_EVENTS, SOURCES

from .blueprints import (
    bundle_revision_from_cr,
    pristine_blueprint,
    suite_document,
)
from .cluster import (
    COMPANY,
    COMPANY_NS,
    HELLO_SAAS,
    console,
    console_env,
    events_since,
    kubectl,
    psql_json,
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
    # Preview of the applied document — the blueprint the suite leaves
    # applied: a converged cluster diffs empty.
    pristine = _poseidon(
        env, "blueprint", "preview", str(suite_document(pristine_blueprint()))
    )
    assert pristine.returncode == 0, pristine.stderr
    assert f"{COMPANY}: no changes" in pristine.stdout, pristine.stdout
    assert "budget:" in pristine.stdout

    # Preview of an edit of the applied document: exactly the edit, nothing
    # else (its repository is the company's own, which cannot change).
    document = yaml.safe_load(suite_document(pristine_blueprint()).read_text())
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
    # leaves applied — the pristine blueprint, the example on the suite's
    # repository and clock with review off the loop's own initiative.
    from blueprint import load_blueprint
    from compiler import compile_blueprint
    from registry import pack

    plan = compile_blueprint(load_blueprint(suite_document(pristine_blueprint())))
    _, revision = pack(dict(plan.bundle.files))
    assert revision == bundle_revision_from_cr(), (
        "the cluster serves a bundle this blueprint + compiler do not produce"
    )


# -- 2 durable: the killed run's own leak check ------------------------------


def test_the_killed_run_left_no_sandbox_registration(implement_run: dict) -> None:
    """The happy-path fixture kills the worker mid-run; test_happy_path
    shows its crew and its pods swept. Here the clause is closed on what
    the run registered: its sandbox registration is terminated, so the
    gateway serves the run nothing."""
    run_id = implement_run["run_id"]
    registrations = psql_json(
        "SELECT COALESCE(json_agg(r), '[]'::json) FROM ("
        "SELECT run_id, terminated_at FROM registry.sandbox_registrations"
        " WHERE run_id = :'run_id') r",
        run_id=run_id,
    )
    assert registrations, "the run never registered a sandbox"
    for row in registrations:
        assert row["terminated_at"], f"leaked sandbox registration: {row}"


# -- 6 one standing agent, from the registry's point of view -----------------


def test_the_registry_roster_is_the_blueprints_one_standing_agent(stack) -> None:
    """`exactly one standing AgentInstance` is asserted against the CRs in
    test_reconciler_lifecycle; the registry — which is what gateway authn
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
    ], f"the registry's standing agents are not the blueprint's: {rows}"
    # The console reads the same roster, the conductor marked.
    agents = console().agents().agents
    assert [(a.agent_id, a.standing_name, a.role, a.conductor) for a in agents] == [
        ("pm-1", "pm", "pm", True)
    ], agents

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


# -- 4 governance: every audit source, real, in this suite execution ---------


def test_all_six_audit_sources_are_real_in_this_suite(
    stack, db_session_start: str
) -> None:
    """Criterion 4's audit clause with no seeding anywhere: within this
    suite execution (fenced by the database's own clock at session start),
    real producers wrote every source in the closed audit-source vocabulary —
    gateway tool decisions, llm-gateway metering/cap refusals, sandbox-infra
    egress denies, process-gate escalations/verdicts, process-lifecycle
    mint/revoke, console acts — and each source correlates to runs by
    run_id.

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
