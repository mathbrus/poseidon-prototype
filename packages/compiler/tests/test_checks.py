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
    problems = [p for p in problems_of(blueprint)
                if "conductor" in p and "implement" in p]
    # Two independent guards agree: the general hired-conductor check and
    # the step-19 spawner-only rule.
    assert any("not a hired standing role" in p for p in problems)
    assert any("not a hired spawner" in p for p in problems)


def test_workflow_cannot_bind_to_an_unknown_role(blueprint):
    blueprint["workflows"]["implement"]["conductor"] = "cto"
    assert any("cto" in p for p in problems_of(blueprint))


def test_placeholder_at_count_zero_is_not_a_conductor(blueprint):
    """A `count: 0` role documents a growth path; nothing may bind to it."""
    blueprint["workflows"]["intake"]["conductor"] = "analyst"
    assert any("analyst" in p for p in problems_of(blueprint))


def test_unknown_tool_scope_fails_at_compile(blueprint):
    blueprint["tools"]["pm"]["allow"].append("issues.purge")
    assert any("issues.purge" in p for p in problems_of(blueprint))


def test_platform_tool_cannot_be_granted_to_a_role(blueprint):
    blueprint["tools"]["pm"]["allow"].append("git.merge_pr")
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_scope_for_a_role_nobody_uses_is_dead_config(blueprint):
    blueprint["tools"]["analyst"] = {"allow": ["issues.read"]}
    assert any("analyst" in p and "never apply" in p for p in problems_of(blueprint))


def test_unknown_role_template_is_a_compile_error(blueprint):
    """compile_blueprint accepts unvalidated dicts; an unknown template must
    be a CompileError problem, not a KeyError halfway through emission."""
    blueprint["departments"]["roles"]["pm"]["template"] = "boss"
    assert any("role catalog" in p for p in problems_of(blueprint))


def test_unknown_crew_name_is_a_compile_error(blueprint):
    blueprint["crews"]["hotfix-swat"] = blueprint["crews"].pop("feature-review")
    assert any("crews.hotfix-swat" in p for p in problems_of(blueprint))


def test_spawn_authority_cannot_be_widened(blueprint):
    """The analyst template grants no spawn authority; a blueprint granting
    it anyway is an error, not a silent drop — a workflow might depend on
    the grant."""
    blueprint["departments"]["roles"]["analyst"]["may_spawn"] = {
        "crew": ["feature-delivery"]
    }
    (problem,) = [p for p in problems_of(blueprint) if "spawn authority" in p]
    assert "analyst" in problem


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


def test_monthly_budget_for_an_unhired_role_is_caught(blueprint):
    blueprint["budgets"]["per_role_monthly"]["sdet"] = {"usd": 10}
    assert any("sdet" in p for p in problems_of(blueprint))


def test_child_scope_need_not_be_a_subset_of_the_conductor(blueprint):
    """The PM cannot call git.open_pr, yet the crew it spawns may.
    Dropping ⊆-parent is deliberate — spawn authority is not a
    capability ceiling — so this must compile."""
    pm_tools = set(blueprint["tools"]["pm"]["allow"])
    executor = set(blueprint["crews"]["feature-delivery"]["members"]["executor"]["tools"])
    assert "git.open_pr" in executor and "git.open_pr" not in pm_tools
    assert compile_blueprint(blueprint) is not None


def test_standing_executor_is_a_compile_error(bridge):
    """The strict lean-roster stance — no standing executor counts;
    workflow nodes bind to spawners only. A standing-executor shape is a
    compile error."""
    with pytest.raises(CompileError) as err:
        compile_blueprint(bridge)
    assert "non-spawner template 'backend-engineer'" in str(err.value)


def test_lean_roster_compiles_to_exactly_one_instance(lean):
    """What step 19's firing diff converges to."""
    plan = compile_blueprint(lean)
    assert [i.hire for i in plan.instances] == ["pm"]
