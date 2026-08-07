"""Blueprint validation: the shipped example validates, and representative
invalid documents fail with errors that say where and what to write instead.

Both properties matter, so the
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


def test_hello_saas_is_the_lean_profile(blueprint):
    """The PM is the only standing hire; the analyst placeholder documents
    growth at count 0; no standing executor exists in the document at
    all."""
    roles = blueprint["departments"]["roles"]
    assert roles["pm"]["count"] == 1
    assert roles["analyst"]["count"] == 0
    assert "backend-engineer" not in roles


def test_pm_holds_no_executor_tools(blueprint):
    """Spawn authority is not permission to wield the crew's tools —
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
    """git.merge_pr is the orchestrator's after the gate. A blueprint
    that could grant it would make the human merge gate optional."""
    blueprint["tools"]["pm"]["allow"].append("git.merge_pr")
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_crew_member_cannot_hold_the_platform_merge_tool(blueprint):
    members = blueprint["crews"]["feature-delivery"]["members"]
    members["executor"]["tools"].append("git.merge_pr")
    assert any("platform identity" in p for p in problems_of(blueprint))


def test_unknown_crew_name_is_caught(blueprint):
    """A crews: key outside the catalog used to validate cleanly and then
    resolve against nothing in the compiler."""
    blueprint["crews"]["hotfix-swat"] = copy.deepcopy(
        blueprint["crews"]["feature-review"]
    )
    (problem,) = [
        p for p in problems_of(blueprint) if p.startswith("crews.hotfix-swat:")
    ]
    assert "crew template" in problem
    assert "feature-delivery" in problem  # the known values are shown


def test_unknown_crew_member_name_is_caught(blueprint):
    members = blueprint["crews"]["feature-review"]["members"]
    members["stenographer"] = copy.deepcopy(members["reviewer"])
    (problem,) = [p for p in problems_of(blueprint) if "stenographer" in p]
    assert "not a member" in problem
    assert "reviewer" in problem  # the catalog's members are shown


def test_crew_member_template_must_match_the_catalog(blueprint):
    """The catalog defines who the reviewer is; a document swapping in a
    different (but existing) template is wrong about the crew, not hiring."""
    blueprint["crews"]["feature-review"]["members"]["reviewer"][
        "role_template"
    ] = "backend-engineer"
    (problem,) = [
        p for p in problems_of(blueprint) if "does not match the catalog" in p
    ]
    assert "'sdet'" in problem


def test_crew_member_tools_cannot_widen_the_catalog(blueprint):
    """The planner-is-read-only contract is a catalog property: a blueprint
    granting the planner a write tool must fail validation, not compile
    into a wider bundle."""
    planner = blueprint["crews"]["feature-delivery"]["members"]["planner"]
    planner["tools"].append("sandbox.write_file")
    (problem,) = [
        p for p in problems_of(blueprint) if "sandbox.write_file" in p
    ]
    assert "never widens" in problem
    assert "sandbox.read_file" in problem  # the allowed values are shown


def test_crew_member_tools_may_narrow_the_catalog(blueprint):
    executor = blueprint["crews"]["feature-delivery"]["members"]["executor"]
    executor["tools"] = ["sandbox.read_file", "sandbox.run_tests"]
    assert validate_blueprint(blueprint) is blueprint


# -- the end-state roster still validates ---------------------------------


def test_lean_roster_profile_validates(blueprint):
    """The checked-in document IS the lean profile now; the compile error
    that rejects standing executors lives in the compiler, not here — a
    bridge-shaped document still *validates* structurally."""
    assert validate_blueprint(blueprint) is blueprint
    bridge = copy.deepcopy(blueprint)
    bridge["departments"]["roles"]["backend-engineer"] = {
        "template": "backend-engineer", "count": 1,
    }
    assert validate_blueprint(bridge) is bridge
