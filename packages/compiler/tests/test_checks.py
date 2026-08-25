"""Compile-time validations: each one is a governance mistake that must fail
here rather than at the first tool call in production."""

import pytest

from compiler import CompileError, compile_blueprint


def problems_of(blueprint: dict) -> list[str]:
    with pytest.raises(CompileError) as caught:
        compile_blueprint(blueprint)
    return caught.value.problems


def test_hello_saas_compiles(blueprint):
    assert compile_blueprint(blueprint).company.name == "hello-saas"


def test_workflow_needs_a_hired_conductor(blueprint):
    blueprint["departments"]["roles"]["pm"]["count"] = 0
    (problem,) = [
        p for p in problems_of(blueprint) if "conductor" in p and "implement" in p
    ]
    assert "not a hired spawner" in problem


def test_workflow_cannot_bind_to_an_unknown_role(blueprint):
    blueprint["workflows"]["implement"]["conductor"] = "cto"
    assert any("cto" in p for p in problems_of(blueprint))


def test_unknown_tool_scope_fails_at_compile(blueprint):
    blueprint["tools"]["pm"]["allow"].append("issues.purge")
    assert any("issues.purge" in p for p in problems_of(blueprint))


def test_platform_tool_cannot_be_granted_to_a_role(blueprint):
    blueprint["tools"]["pm"]["allow"].append("git.merge_pr")
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_scope_for_a_role_nobody_uses_is_dead_config(blueprint):
    blueprint["tools"]["auditor"] = {"allow": ["issues.read"]}
    assert any("auditor" in p and "never apply" in p for p in problems_of(blueprint))


def test_unknown_role_template_is_a_compile_error(blueprint):
    """compile_blueprint accepts unvalidated dicts; an unknown template must
    be a CompileError problem, not a KeyError halfway through emission."""
    blueprint["departments"]["roles"]["pm"]["template"] = "boss"
    assert any("role catalog" in p for p in problems_of(blueprint))


def test_unknown_crew_name_is_a_compile_error(blueprint):
    blueprint["crews"]["hotfix-swat"] = blueprint["crews"].pop("feature-review")
    assert any("crews.hotfix-swat" in p for p in problems_of(blueprint))


def test_spawn_authority_cannot_be_widened(blueprint):
    """Granting a role a crew its template does not permit is an error, not
    a silent drop — a workflow might depend on the grant. The sdet template
    grants no spawn authority."""
    blueprint["departments"]["roles"]["future-lead"] = {
        "template": "sdet",
        "count": 0,
        "may_spawn": {"crew": ["feature-delivery"]},
    }
    (problem,) = [p for p in problems_of(blueprint) if "spawn authority" in p]
    assert "future-lead" in problem


def test_crew_member_tools_cannot_widen_the_catalog(blueprint):
    planner = blueprint["crews"]["feature-delivery"]["members"]["planner"]
    planner["tools"].append("sandbox.write_file")
    assert any("never widens" in p for p in problems_of(blueprint))


def test_invalid_branch_pattern_fails_at_compile(blueprint):
    """A malformed allowed_pattern would otherwise surface as an OPA
    bundle-load failure long after apply."""
    blueprint["policies"]["branch_protection"]["allowed_pattern"] = "feature/("
    assert any("allowed_pattern" in p for p in problems_of(blueprint))


def test_may_spawn_must_name_a_declared_crew(blueprint):
    blueprint["departments"]["roles"]["pm"]["may_spawn"]["crew"].append("feature-review")
    del blueprint["crews"]["feature-review"]
    assert any("feature-review" in p for p in problems_of(blueprint))


def test_workflow_crew_must_be_spawnable_by_its_conductor(blueprint):
    """Otherwise the workflow compiles but can never be fulfilled."""
    blueprint["departments"]["roles"]["pm"]["may_spawn"]["crew"] = ["feature-review"]
    (problem,) = [p for p in problems_of(blueprint) if "may not spawn" in p]
    assert "feature-delivery" in problem


def test_crew_member_needs_a_real_allow_list(blueprint):
    blueprint["crews"]["feature-delivery"]["members"]["executor"]["tools"] = []
    assert any("must not be empty" in p for p in problems_of(blueprint))


def test_enabled_workflows_need_a_run_budget(blueprint):
    blueprint["budgets"]["per_run"]["usd"] = 0
    assert any("per_run" in p for p in problems_of(blueprint))


def test_child_scope_need_not_be_a_subset_of_the_conductor(blueprint):
    """The PM cannot call git.open_pr, yet the crew it spawns may.
    Dropping ⊆-parent is deliberate — spawn authority is not a
    capability ceiling — so this must compile."""
    pm_tools = set(blueprint["tools"]["pm"]["allow"])
    executor = set(blueprint["crews"]["feature-delivery"]["members"]["executor"]["tools"])
    assert "git.open_pr" in executor and "git.open_pr" not in pm_tools
    assert compile_blueprint(blueprint) is not None


def test_standing_executor_is_a_compile_error(standing_executor):
    """The strict lean-roster stance — no standing executor counts;
    workflow nodes bind to spawners only. A standing-executor shape is a
    compile error."""
    with pytest.raises(CompileError) as err:
        compile_blueprint(standing_executor)
    assert "non-spawner template 'backend-engineer'" in str(err.value)


def test_lean_roster_compiles_to_exactly_one_instance(blueprint):
    """The standing roster is the PM alone; execution is ephemeral crews."""
    plan = compile_blueprint(blueprint)
    assert [i.hire for i in plan.instances] == ["pm"]


# -- the conduct heartbeat -------------------------------------------------


def test_conduct_section_compiles(conducted):
    assert compile_blueprint(conducted).conduct is not None


def test_conduct_conductor_must_be_a_hired_spawner(conducted):
    # sdet is a real template but crew-only — never a hired standing role.
    conducted["conduct"]["conductor"] = "sdet"
    (problem,) = [p for p in problems_of(conducted) if "conduct.conductor" in p]
    assert "not a hired spawner" in problem


def test_conduct_may_start_needs_an_enabled_workflow(conducted):
    conducted["workflows"]["implement"]["enabled"] = False
    (problem,) = [p for p in problems_of(conducted) if "conduct.may_start" in p]
    assert "implement" in problem and "enabled" in problem


def test_conduct_daily_cap_must_fund_a_run(conducted):
    """A daily cap below the per-run budget makes the heartbeat dead
    configuration — an error, like every other dead declaration."""
    conducted["conduct"]["daily_cap"]["usd"] = 1
    (problem,) = [p for p in problems_of(conducted) if "conduct.daily_cap" in p]
    assert "cannot fund a single run" in problem


# -- the merge delegation ---------------------------------------------------


def test_delegated_merge_compiles_the_gate_into_the_bundle(blueprint):
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "resolved_by": "policy",
        "delegate": {"paths": ["src/", "tests/"], "max_reworks": 2},
    }
    plan = compile_blueprint(blueprint)
    assert "merge_gate.rego" in plan.bundle.files


def test_no_delegation_compiles_no_merge_gate_file(blueprint):
    """Revocation is a blueprint diff: a human-resolved gate emits no
    merge.resolve authority at all, so the decision falls to default deny."""
    assert "merge_gate.rego" not in compile_blueprint(blueprint).bundle.files


def test_compiler_fails_closed_on_a_carved_out_delegation(blueprint):
    """`compile_blueprint` accepts unvalidated dicts — the undelegatable
    carve-out must refuse here too, not only in validate_blueprint."""
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "resolved_by": "policy",
        "delegate": {"paths": [".github/"], "max_reworks": 2},
    }
    assert any("can never be delegated" in p for p in problems_of(blueprint))


def test_compiler_fails_closed_on_policy_without_a_delegation(blueprint):
    blueprint["workflows"]["review"]["gates"]["merge"] = {"resolved_by": "policy"}
    assert any("delegate" in p for p in problems_of(blueprint))


# -- the contract tree --------------------------------------------------------


def test_compiler_fails_closed_on_a_delegated_contract_tree(blueprint):
    """The document-level refusal repeated at compile, the carve-out
    posture: the oracle never merges under the delegation it certifies."""
    blueprint["policies"]["contract_tests"] = {
        "paths": ["tests/contract/"],
        "roles": ["sdet"],
    }
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "resolved_by": "policy",
        "delegate": {"paths": ["src/", "tests/"], "max_reworks": 2},
    }
    assert any("contract_tests" in p and "oracle" in p for p in problems_of(blueprint))


def test_compiler_fails_closed_on_an_unknown_contract_owner(blueprint):
    """An owner outside the injected catalog would compile a tree nobody
    can ever deliver into — the spec frozen by typo."""
    blueprint["policies"]["contract_tests"] = {
        "paths": ["tests/contract/"],
        "roles": ["spec-author"],
    }
    assert any(
        "contract_tests.roles" in p and "role catalog" in p
        for p in problems_of(blueprint)
    )


def test_compiler_fails_closed_on_a_denied_contract_tree(blueprint):
    blueprint["policies"]["deny_paths"] = [".github/workflows/", "specs/"]
    blueprint["policies"]["contract_tests"] = {
        "paths": ["specs/contract/"],
        "roles": ["sdet"],
    }
    assert any("no role could ever deliver" in p for p in problems_of(blueprint))
