"""The emitted plan, pinned by golden files.

Regenerate deliberately (never reflexively — the diff is the review):

    uv run python -m compiler.regolden
"""

import json

from compiler import compile_blueprint

from conftest import GOLDEN


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


def test_standing_roster_excludes_the_placeholder(blueprint):
    plan = compile_blueprint(blueprint)
    assert "analyst" not in {i.hire for i in plan.instances}


def test_agent_specs_carry_no_secrets(blueprint):
    """§4.1: no secrets in agent specs — credentials arrive at runtime as
    projected tokens, never baked into a CR."""
    blob = json.dumps(compile_blueprint(blueprint).as_dict()).lower()
    for smell in ("token", "password", "secret", "api_key", "apikey", "ghp_", "sk-"):
        assert smell not in blob, f"{smell!r} appears in the plan"


def test_pm_scope_is_narrowed_not_widened(blueprint):
    """A blueprint may narrow a catalog template; naming a tool the catalog
    does not grant must not widen it."""
    blueprint["tools"]["pm"]["allow"].append("sandbox.exec")
    plan = compile_blueprint(blueprint)
    pm = next(i for i in plan.instances if i.hire == "pm")
    assert "sandbox.exec" not in pm.tools


def test_crew_members_are_specs_not_instances(blueprint):
    """Ephemeral members exist only inside a run (§4.4 rule 2): the plan
    describes them, the operator never creates them."""
    plan = compile_blueprint(blueprint)
    members = {(m.crew, m.member) for m in plan.crew_members}
    assert members == {
        ("feature-delivery", "planner"),
        ("feature-delivery", "executor"),
        ("feature-review", "reviewer"),
    }
    planner = next(m for m in plan.crew_members if m.member == "planner")
    assert planner.needs_sandbox is False  # LLM-only, per the planner contract


def test_gate_config_comes_from_the_blueprint(blueprint):
    plan = compile_blueprint(blueprint)
    review = next(w for w in plan.workflows if w.name == "review")
    assert review.gates["merge"] == "human_approval"
    assert review.drain["on_conductor_drain"] == "abort_crew"


def test_infra_ir_feeds_both_consumers_from_one_list(blueprint):
    """§5.4: the sandbox caps and the proxy allowlist are projections of one
    IR — hand-authoring either is the bug this replaces."""
    plan = compile_blueprint(blueprint)
    assert plan.infra.sandbox.egress_allow == plan.infra.egress_proxy_allowlist
    assert plan.infra.sandbox.image == "poseidon-sandbox"
    assert plan.infra.network_policy.sandbox_egress_to == ("egress-proxy", "kube-dns")


def test_empty_egress_allowlist_means_zero_egress(blueprint):
    del blueprint["stack"]["toolchain"]["egress_allow"]
    plan = compile_blueprint(blueprint)
    assert plan.infra.egress_proxy_allowlist == ()
