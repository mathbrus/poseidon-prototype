"""Blueprint validation: the shipped example validates, and representative
invalid documents fail with errors that say where and what to write instead.

The done-when for plan step 7 is exactly those two properties, so the
invalid cases assert on message *content*, not just on the raising.
"""

import copy
from pathlib import Path

import pytest
import yaml

from schema import load_blueprint, validate_blueprint
from schema.validate import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[3]
HELLO_SAAS = REPO_ROOT / "examples" / "hello-saas.yaml"


@pytest.fixture
def blueprint() -> dict:
    return yaml.safe_load(HELLO_SAAS.read_text())


def problems_of(document: dict) -> list[str]:
    with pytest.raises(ValidationError) as caught:
        validate_blueprint(document)
    return caught.value.problems


# -- the shipped example --------------------------------------------------


def test_hello_saas_validates():
    assert load_blueprint(HELLO_SAAS)["company"]["name"] == "hello-saas"


def test_hello_saas_is_the_bridge_profile(blueprint):
    """The transitional engineer is present and marked for deletion at plan
    step 19; the analyst placeholder is hired at zero."""
    roles = blueprint["departments"]["roles"]
    assert roles["pm"]["count"] == 1
    assert roles["backend-engineer"]["count"] == 1
    assert roles["analyst"]["count"] == 0
    assert "TRANSITIONAL" in HELLO_SAAS.read_text()


def test_pm_holds_no_executor_tools(blueprint):
    """§4.5: spawn authority is not permission to wield the crew's tools —
    a gateway deny, not prompt hope, and it starts in the blueprint."""
    pm_tools = set(blueprint["tools"]["pm"]["allow"])
    assert not any(t.startswith("sandbox.") for t in pm_tools)
    assert "git.open_pr" not in pm_tools
    assert {"crew.request", "issues.create"} <= pm_tools


def test_review_merge_gate_is_human_approval(blueprint):
    assert blueprint["workflows"]["review"]["gates"]["merge"] == "human_approval"


def test_planner_is_read_only(blueprint):
    planner = blueprint["crews"]["feature-delivery"]["members"]["planner"]
    assert planner["needs_sandbox"] is False
    assert set(planner["tools"]) == {
        "sandbox.read_file",
        "sandbox.list_dir",
        "issues.read",
    }


# -- structural errors ----------------------------------------------------


def test_missing_section_is_named(blueprint):
    del blueprint["budgets"]
    assert any("budgets" in p for p in problems_of(blueprint))


def test_typo_in_a_key_is_rejected_not_ignored(blueprint):
    blueprint["company"]["mision"] = "typo"
    (problem,) = [p for p in problems_of(blueprint) if "mision" in p]
    assert problem.startswith("company:")
    assert "spelling" in problem


def test_unknown_tool_lists_what_is_allowed(blueprint):
    blueprint["tools"]["pm"]["allow"].append("issues.delete")
    (problem,) = [p for p in problems_of(blueprint) if "issues.delete" in p]
    assert "tools.pm.allow" in problem
    assert "issues.create" in problem  # the allowed values are shown


def test_unknown_model_tier_lists_the_tiers(blueprint):
    blueprint["departments"]["roles"]["pm"]["model_tier"] = "gigantic"
    (problem,) = [p for p in problems_of(blueprint) if "gigantic" in p]
    assert "'small'" in problem and "'frontier'" in problem


def test_crew_member_without_an_allow_list_is_rejected(blueprint):
    del blueprint["crews"]["feature-delivery"]["members"]["executor"]["tools"]
    problems = problems_of(blueprint)
    assert any("executor" in p and "tools" in p for p in problems)


def test_every_problem_is_reported_together(blueprint):
    del blueprint["budgets"]
    del blueprint["stack"]
    problems = problems_of(blueprint)
    assert len(problems) >= 2  # not one-per-run


def test_a_non_mapping_document_is_rejected():
    assert "expected a YAML mapping" in problems_of(["not", "a", "blueprint"])[0]


# -- catalog cross-checks -------------------------------------------------


def test_unknown_role_template_lists_the_catalog(blueprint):
    blueprint["departments"]["roles"]["pm"]["template"] = "product-manager"
    (problem,) = [p for p in problems_of(blueprint) if "product-manager" in p]
    assert "role catalog" in problem
    assert "pm" in problem and "sdet" in problem


def test_analyst_placeholder_cannot_be_hired(blueprint):
    blueprint["departments"]["roles"]["analyst"]["count"] = 1
    (problem,) = [p for p in problems_of(blueprint) if "analyst" in p]
    assert "count: 0" in problem


def test_unknown_crew_in_may_spawn_is_caught(blueprint):
    blueprint["departments"]["roles"]["pm"]["may_spawn"]["crew"].append("hotfix-swat")
    (problem,) = [p for p in problems_of(blueprint) if "hotfix-swat" in p]
    assert "feature-delivery" in problem


def test_unknown_crew_member_template_is_caught(blueprint):
    blueprint["crews"]["feature-review"]["members"]["reviewer"]["role_template"] = "qa"
    assert any("role_template" in p for p in problems_of(blueprint))


def test_company_role_cannot_hold_the_platform_merge_tool(blueprint):
    """O-1 A: git.merge_pr is the orchestrator's after the gate. A blueprint
    that could grant it would make the human merge gate optional."""
    blueprint["tools"]["pm"]["allow"].append("git.merge_pr")
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_crew_member_cannot_hold_the_platform_merge_tool(blueprint):
    members = blueprint["crews"]["feature-delivery"]["members"]
    members["executor"]["tools"].append("git.merge_pr")
    assert any("platform identity" in p for p in problems_of(blueprint))


# -- the end-state roster still validates ---------------------------------


def test_lean_roster_profile_validates(blueprint):
    """Step 19's firing diff removes the transitional engineer; the result
    must remain a valid document (the stricter compile error that rejects
    standing executors lands in the compiler, not here)."""
    lean = copy.deepcopy(blueprint)
    del lean["departments"]["roles"]["backend-engineer"]
    assert validate_blueprint(lean) is lean
