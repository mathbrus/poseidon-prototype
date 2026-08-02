"""Compile-time validations: each one is a governance mistake that must fail
here rather than at the first tool call in production (§4.1)."""

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
    (problem,) = [p for p in problems_of(blueprint) if "conductor" in p and "implement" in p]
    assert "not a hired standing role" in problem


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
    """§4.5 rule 5: the PM cannot call git.open_pr, yet the crew it spawns
    may. Dropping ⊆-parent is deliberate — spawn authority is not a
    capability ceiling — so this must compile."""
    pm_tools = set(blueprint["tools"]["pm"]["allow"])
    executor = set(blueprint["crews"]["feature-delivery"]["members"]["executor"]["tools"])
    assert "git.open_pr" in executor and "git.open_pr" not in pm_tools
    assert compile_blueprint(blueprint) is not None


def test_transitional_standing_executor_still_compiles(blueprint):
    """The M2→M3 bridge hires one (sequencing choice 7); the strict
    spawner-only error lands at plan step 19, not here."""
    plan = compile_blueprint(blueprint)
    assert {i.hire for i in plan.instances} == {"pm", "backend-engineer"}


def test_lean_roster_compiles_to_exactly_one_instance(lean):
    """What step 19's firing diff converges to."""
    plan = compile_blueprint(lean)
    assert [i.hire for i in plan.instances] == ["pm"]
