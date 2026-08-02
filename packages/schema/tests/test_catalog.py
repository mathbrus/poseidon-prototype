"""The versioned catalogs: the invariants later steps compile against.

These are contract tests, not coverage — each one is a rule some other step
(9, 15, 16, 19) will assume it can rely on.
"""

from schema.catalog import CREW_TEMPLATES, MODEL_TIERS, ROLE_TEMPLATES
from schema.tools import KNOWN_TOOLS, PLATFORM_ONLY


def test_catalog_covers_the_four_mvp_templates():
    assert set(ROLE_TEMPLATES) == {"pm", "backend-engineer", "sdet", "analyst"}


def test_every_template_scope_is_a_known_tool():
    for template in ROLE_TEMPLATES.values():
        assert set(template.tools) <= KNOWN_TOOLS, template.name


def test_no_template_carries_a_platform_only_tool():
    for template in ROLE_TEMPLATES.values():
        assert not set(template.tools) & PLATFORM_ONLY, template.name


def test_every_template_names_a_real_model_tier():
    for template in ROLE_TEMPLATES.values():
        assert template.model_tier in MODEL_TIERS, template.name


def test_pm_conducts_but_does_not_execute():
    pm = ROLE_TEMPLATES["pm"]
    assert set(pm.may_spawn) == {"feature-delivery", "feature-review"}
    assert not any(t.startswith("sandbox.") for t in pm.tools)
    assert "git.open_pr" not in pm.tools
    # No private memory store: cross-run state is tickets + registry runs.
    assert pm.runtime_hints["memory"] == "none"


def test_analyst_is_a_placeholder_with_no_tools():
    analyst = ROLE_TEMPLATES["analyst"]
    assert analyst.placeholder_only
    assert analyst.tools == ()


def test_executor_templates_are_not_placeholders():
    """backend-engineer and sdet stay hireable-in-schema through the M2→M3
    bridge; the lean-roster refusal is a compiler error at step 19."""
    assert not ROLE_TEMPLATES["backend-engineer"].placeholder_only
    assert not ROLE_TEMPLATES["sdet"].placeholder_only


def test_crew_templates_are_the_two_mvp_crews():
    assert set(CREW_TEMPLATES) == {"feature-delivery", "feature-review"}


def test_feature_delivery_is_planner_plus_one_executor():
    members = CREW_TEMPLATES["feature-delivery"].members
    assert set(members) == {"planner", "executor"}
    assert all(m.role_template == "backend-engineer" for m in members.values())


def test_planner_is_llm_only_and_write_free():
    planner = CREW_TEMPLATES["feature-delivery"].members["planner"]
    assert planner.needs_sandbox is False
    assert set(planner.tools) == {
        "sandbox.read_file",
        "sandbox.list_dir",
        "issues.read",
    }


def test_every_crew_member_has_an_explicit_allow_list():
    for crew in CREW_TEMPLATES.values():
        for name, member in crew.members.items():
            assert member.tools, f"{crew.name}.{name} has no allow-list"
            assert set(member.tools) <= KNOWN_TOOLS
            assert not set(member.tools) & PLATFORM_ONLY


def test_members_inherit_the_run_budget():
    for crew in CREW_TEMPLATES.values():
        assert crew.budget_inheritance == "run"


def test_executor_can_deliver_and_reviewer_cannot():
    executor = CREW_TEMPLATES["feature-delivery"].members["executor"]
    reviewer = CREW_TEMPLATES["feature-review"].members["reviewer"]
    assert "git.open_pr" in executor.tools
    assert "git.open_pr" not in reviewer.tools
    assert {"git.read", "git.comment"} <= set(reviewer.tools)
