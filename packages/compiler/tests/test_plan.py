"""The emitted plan, pinned by golden files.

Regenerate deliberately (never reflexively — the diff is the review):

    uv run python -m compiler.regolden
"""

import json

from conftest import GOLDEN

from compiler import compile_blueprint


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
    the standing agents is the standing_roles with count > 0, the PM alone here."""
    blueprint["standing"]["future-sdet"] = {"template": "sdet", "count": 0}
    plan = compile_blueprint(blueprint)
    assert {i.standing_role for i in plan.instances} == {"pm"}


def test_agent_specs_carry_no_secrets(blueprint):
    """No secrets in agent specs — credentials arrive at runtime as
    projected tokens, never baked into a CR."""
    blob = json.dumps(compile_blueprint(blueprint).as_dict()).lower()
    for smell in ("token", "password", "secret", "api_key", "apikey", "ghp_", "sk-"):
        assert smell not in blob, f"{smell!r} appears in the plan"


def test_pm_scope_is_narrowed_not_widened(blueprint):
    """A blueprint may narrow a catalog template; naming a tool the catalog
    does not grant must not widen it."""
    blueprint["tools"]["pm"]["allow"].append("sandbox.exec")
    plan = compile_blueprint(blueprint)
    pm = next(i for i in plan.instances if i.standing_role == "pm")
    assert "sandbox.exec" not in pm.tools


def test_ephemeral_agents_are_specs_not_instances(blueprint):
    """Ephemeral agents exist only inside a run: the plan describes
    them, the operator never creates them."""
    plan = compile_blueprint(blueprint)
    agents = {(m.crew, m.crew_role) for m in plan.ephemeral_agents}
    assert agents == {
        ("feature-delivery", "executor"),
        ("feature-review", "reviewer"),
    }


def test_gate_config_comes_from_the_blueprint(blueprint):
    plan = compile_blueprint(blueprint)
    review = next(w for w in plan.workflows if w.name == "review")
    assert review.gates["merge"] == {"resolved_by": "human"}


def test_test_command_rides_the_sandbox_constraints(blueprint):
    """The blueprint names the test command the platform verify gate runs;
    a blueprint that names none gets the sandbox image's own default."""
    assert compile_blueprint(blueprint).infra.sandbox.test_command == "pytest -q"
    blueprint["stack"]["toolchain"]["test_command"] = "make check"
    assert compile_blueprint(blueprint).infra.sandbox.test_command == "make check"
    del blueprint["stack"]["toolchain"]["test_command"]
    assert compile_blueprint(blueprint).infra.sandbox.test_command == "pytest -q"


def test_infra_ir_feeds_both_consumers_from_one_list(blueprint):
    """The sandbox caps and the proxy allowlist are projections of one
    IR — hand-authoring either would be a bug."""
    plan = compile_blueprint(blueprint)
    assert plan.infra.sandbox.egress_allow == plan.infra.egress_proxy_allowlist
    assert plan.infra.sandbox.image == "poseidon-sandbox"
    # The audited proxy is the sandbox's only peer — no cluster DNS, so a
    # resolver cannot be used as an unaudited channel.
    assert plan.infra.network_policy.sandbox_egress_to == ("egress-proxy",)


def test_empty_egress_allowlist_means_zero_egress(blueprint):
    del blueprint["stack"]["toolchain"]["egress_allow"]
    plan = compile_blueprint(blueprint)
    assert plan.infra.egress_proxy_allowlist == ()


def test_a_plan_without_heartbeat_stays_human_started(blueprint):
    """No heartbeat section, no heartbeat artifacts anywhere: the applied
    company is human-started."""
    plan = compile_blueprint(blueprint)
    assert plan.heartbeat is None
    assert plan.as_dict()["heartbeat"] is None
    assert "heartbeat.rego" not in plan.bundle.files


def test_heartbeat_compiles_into_plan_and_bundle(heartbeat_blueprint):
    plan = compile_blueprint(heartbeat_blueprint)
    assert plan.heartbeat.conductor == "pm"
    assert plan.heartbeat.reconcile_minutes == 30
    assert plan.heartbeat.max_concurrent_runs == 1
    assert plan.heartbeat.max_attempts == 2
    assert plan.heartbeat.may_start == ("implement",)
    assert plan.heartbeat.daily_cap_usd == 20.0
    assert "heartbeat.rego" in plan.bundle.files
    assert plan.as_dict()["heartbeat"]["may_start"] == ["implement"]
