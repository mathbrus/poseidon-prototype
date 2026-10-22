"""The emitted plan, pinned by golden files.

Regenerate deliberately (never reflexively — the diff is the review):

    uv run python -m compiler.regolden
"""

import json

import pytest
import yaml
from conftest import GOLDEN, REPO_ROOT

from blueprint.catalog import ROLES
from compiler import compile_blueprint
from compiler.errors import CompileError


def test_plan_matches_the_golden_file(blueprint):
    plan = compile_blueprint(blueprint).as_dict()
    expected = json.loads((GOLDEN / "hello-saas.plan.json").read_text())
    assert plan == expected


def test_bundle_matches_the_golden_files(blueprint):
    """The golden bundle directory is the reviewable form of the emitted
    Rego; this is what ties it to the emitter (it is read by nothing else)."""
    plan = compile_blueprint(blueprint)
    bundle_dir = GOLDEN / "hello-saas-bundle"
    on_disk = {p.name: p.read_text() for p in bundle_dir.glob("*.rego")}
    assert on_disk == {**plan.bundle.files, **plan.platform_bundle.files}


def test_compilation_is_deterministic(blueprint):
    """Same inputs, byte-identical plan: the diff at preview is only
    meaningful if nothing wobbles between runs."""
    first = json.dumps(compile_blueprint(blueprint).as_dict(), sort_keys=True)
    second = json.dumps(compile_blueprint(blueprint).as_dict(), sort_keys=True)
    assert first == second


# -- what the plan must contain -------------------------------------------


def test_standing_roster_is_only_the_count_positive_hires(blueprint):
    """A `count: 0` role documents a growth path and produces no instance —
    the standing agents are the hires with count > 0, the PM alone here."""
    blueprint["standing"]["future-reviewer"] = {"role": "reviewer", "count": 0}
    plan = compile_blueprint(blueprint)
    assert {i.standing_name for i in plan.instances} == {"pm"}


def test_agent_specs_carry_no_secrets(blueprint):
    """No secrets in agent specs — credentials arrive at runtime as
    projected tokens, never baked into a CR."""
    blob = json.dumps(compile_blueprint(blueprint).as_dict()).lower()
    for smell in ("token", "password", "secret", "api_key", "apikey", "ghp_", "sk-"):
        assert smell not in blob, f"{smell!r} appears in the plan"


def test_the_reviewer_holds_what_the_review_procedure_uses(blueprint):
    """With no scope declared, the reviewer's compiled tools are its
    catalog's: a shell in its own sandbox, and nothing that writes files
    or carries work out of it."""
    assert "reviewer" not in blueprint["tools"]
    plan = compile_blueprint(blueprint)
    reviewer = next(a for a in plan.ephemeral_agents if a.role == "reviewer")
    assert set(reviewer.tools) == set(ROLES["reviewer"].tools)
    assert "sandbox.exec" in reviewer.tools
    for tool in ("sandbox.write_file", "sandbox.edit_file", "sandbox.git_bundle"):
        assert tool not in reviewer.tools
    assert "git.open_pr" not in reviewer.tools


def test_a_tool_scope_narrows_standing_and_ephemeral_agents_alike(blueprint):
    """One narrowing site: `tools.<role>.allow` applies to every agent of
    the role, and the compiled crew spec carries the narrowed list."""
    narrowed = ["sandbox.read_file", "sandbox.run_tests", "git.read", "git.comment"]
    blueprint["tools"]["reviewer"] = {"allow": narrowed}
    plan = compile_blueprint(blueprint)
    reviewer = next(a for a in plan.ephemeral_agents if a.role == "reviewer")
    assert set(reviewer.tools) == set(narrowed)
    # A role with no scope declared gets its catalog tools.
    implementer = next(a for a in plan.ephemeral_agents if a.role == "implementer")
    assert "git.open_pr" in implementer.tools


def test_ephemeral_agents_are_specs_not_instances(blueprint):
    """Ephemeral agents exist only inside a run: the plan describes
    them, the reconciler never creates them."""
    plan = compile_blueprint(blueprint)
    agents = {(m.crew, m.role) for m in plan.ephemeral_agents}
    assert agents == {
        ("feature-delivery", "implementer"),
        ("feature-review", "reviewer"),
        ("stewardship", "steward"),
    }


def test_gate_config_comes_from_the_blueprint(blueprint):
    plan = compile_blueprint(blueprint)
    review = next(w for w in plan.workflows if w.name == "review")
    assert review.gates["merge"] == {"resolved_by": "human", "deadline_minutes": 1440}


def test_test_command_rides_the_sandbox_constraints(blueprint):
    """The blueprint names the test command the platform verify gate runs,
    and it names it — a blueprint without one does not compile."""
    assert compile_blueprint(blueprint).infra.sandbox.test_command == "pytest -q"
    blueprint["stack"]["toolchain"]["test_command"] = "make check"
    assert compile_blueprint(blueprint).infra.sandbox.test_command == "make check"
    del blueprint["stack"]["toolchain"]["test_command"]
    with pytest.raises(CompileError, match="test_command"):
        compile_blueprint(blueprint)


def test_infra_ir_projects_the_toolchain_and_the_proxy_allowlist(blueprint):
    """The sandbox caps and the proxy allowlist are projections of one
    IR — hand-authoring either would be a bug."""
    plan = compile_blueprint(blueprint)
    assert plan.infra.egress_proxy_allowlist == tuple(
        sorted(blueprint["stack"]["toolchain"]["egress_allow"])
    )
    assert plan.infra.sandbox.image == "poseidon-sandbox"
    # The audited proxy is the sandbox's only peer — no cluster DNS, so a
    # resolver cannot be used as an unaudited channel.
    assert plan.infra.network_policy.sandbox_egress_to == ("egress-proxy",)


def test_empty_egress_allowlist_means_zero_egress(blueprint):
    del blueprint["stack"]["toolchain"]["egress_allow"]
    plan = compile_blueprint(blueprint)
    assert plan.infra.egress_proxy_allowlist == ()


def test_heartbeat_compiles_into_plan_and_bundle(blueprint):
    plan = compile_blueprint(blueprint)
    assert plan.heartbeat.conductor == "pm"
    assert plan.heartbeat.reconcile_minutes == 30
    assert plan.heartbeat.max_concurrent_runs == 1
    assert plan.heartbeat.max_attempts == 2
    assert plan.heartbeat.max_open_tickets == 5
    assert plan.heartbeat.may_start == ("implement", "review", "scope")
    assert plan.heartbeat.daily_cap_usd == 20.0
    assert "heartbeat.rego" in plan.bundle.files
    assert plan.as_dict()["heartbeat"]["may_start"] == ["implement", "review", "scope"]


def test_the_company_carries_its_document_paths(blueprint):
    plan = compile_blueprint(blueprint)
    assert plan.company.documents == {
        "architecture": "docs/ARCHITECTURE.md",
        "strategy": "docs/STRATEGY.md",
    }


def test_workflow_nodes_carry_no_conductor(blueprint):
    """One conductor, named on the heartbeat: a workflow node is its crew,
    limits and gates."""
    plan = compile_blueprint(blueprint)
    assert {w.name: w.crew for w in plan.workflows} == {
        "implement": "feature-delivery",
        "review": "feature-review",
    }
    assert not any(hasattr(w, "conductor") for w in plan.workflows)


@pytest.mark.parametrize("example", ["hello-saas.yaml", "hello-saas-delegated.yaml"])
def test_the_company_carries_its_policies_section(example):
    """The policies section rides the Company CR as written, lists sorted
    — what the bundle enforces and what the platform reads to say the
    bounds and compose the contract-verify baseline. The contract tree
    is part of it, not a key of its own."""
    document = yaml.safe_load((REPO_ROOT / "examples" / example).read_text())
    plan = compile_blueprint(document)
    assert plan.company.policies["branch_protection"] == {
        "allowed_pattern": "^feature/[a-z0-9-]+$",
        "protected": ["main"],
    }
    assert plan.company.policies["deny_paths"] == [".github/workflows/"]
    declared = document["policies"].get("contract_tests")
    assert plan.company.policies.get("contract_tests") == declared
    assert "contract_tests" not in plan.as_dict()
