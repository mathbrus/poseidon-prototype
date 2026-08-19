"""The versioned catalogs: the invariants later steps compile against.

These are contract tests, not coverage — each one is a rule some other step
(9, 15, 16, 19) will assume it can rely on.
"""

from schema.catalog import CREW_TEMPLATES, MODEL_TIERS, ROLE_TEMPLATES
from schema.tools import KNOWN_TOOLS, PLATFORM_ONLY


def test_catalog_covers_the_mvp_templates():
    assert set(ROLE_TEMPLATES) == {"pm", "backend-engineer", "sdet", "contract-author"}


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
    assert set(pm.may_spawn) == {
        "contract-authoring",
        "feature-delivery",
        "feature-review",
    }
    assert not any(t.startswith("sandbox.") for t in pm.tools)
    assert "git.open_pr" not in pm.tools
    # No private memory store: cross-run state is tickets + registry runs.
    assert pm.runtime_hints["memory"] == "none"


def test_crew_templates_are_the_catalog_crews():
    assert set(CREW_TEMPLATES) == {
        "contract-authoring",
        "feature-delivery",
        "feature-review",
    }


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


def test_contract_authoring_is_one_deliver_capable_author():
    """The spec's author delivers through the same governed `git.open_pr`
    as any change — deliver-capable unlike `sdet`, and a distinct template
    so `policies.contract_tests` can fence the tree to it, crew-request
    rows never collide with a same-run delivery crew, and reviewer
    independence (keyed on crew_template) holds against it for free."""
    members = CREW_TEMPLATES["contract-authoring"].members
    assert set(members) == {"author"}
    author = members["author"]
    assert author.role_template == "contract-author"
    assert author.needs_sandbox is True  # the acting executor at fulfill
    assert "git.open_pr" in author.tools
    assert "git.open_pr" in ROLE_TEMPLATES["contract-author"].tools
    assert "git.open_pr" not in ROLE_TEMPLATES["sdet"].tools
