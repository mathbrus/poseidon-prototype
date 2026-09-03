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
    """The PM is the only standing agent; no standing implementer exists in the
    document at all."""
    roles = blueprint["standing"]
    assert roles["pm"]["count"] == 1
    assert set(roles) == {"pm"}
    assert "implementer" not in roles  # no standing coder


def test_pm_holds_no_implementer_tools(blueprint):
    """Spawn authority is not permission to wield the crew's tools —
    a gateway deny, not prompt hope, and it starts in the blueprint."""
    pm_tools = set(blueprint["tools"]["pm"]["allow"])
    assert not any(t.startswith("sandbox.") for t in pm_tools)
    assert "git.open_pr" not in pm_tools
    assert {"crew.request", "issues.create"} <= pm_tools


def test_review_merge_gate_is_human_resolved(blueprint):
    assert blueprint["workflows"]["review"]["gates"]["merge"] == {"resolved_by": "human"}


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
    blueprint["standing"]["pm"]["model_tier"] = "gigantic"
    (problem,) = [p for p in problems_of(blueprint) if "gigantic" in p]
    assert "'small'" in problem and "'frontier'" in problem


def test_a_crew_must_list_its_roles(blueprint):
    del blueprint["crews"]["feature-delivery"]["roles"]
    problems = problems_of(blueprint)
    assert any("feature-delivery" in p and "roles" in p for p in problems)


def test_an_empty_tool_scope_is_rejected(blueprint):
    blueprint["tools"]["reviewer"]["allow"] = []
    assert any(
        "tools.reviewer.allow" in p and "empty" in p for p in problems_of(blueprint)
    )


def test_every_problem_is_reported_together(blueprint):
    del blueprint["budgets"]
    del blueprint["stack"]
    problems = problems_of(blueprint)
    assert len(problems) >= 2  # not one-per-run


def test_a_non_mapping_document_is_rejected():
    assert "expected a YAML mapping" in problems_of(["not", "a", "blueprint"])[0]


# -- catalog cross-checks -------------------------------------------------


def test_unknown_role_lists_the_catalog(blueprint):
    blueprint["standing"]["pm"]["role"] = "product-manager"
    (problem,) = [p for p in problems_of(blueprint) if "product-manager" in p]
    assert "role catalog" in problem
    assert "pm" in problem and "reviewer" in problem


def test_unknown_crew_in_may_spawn_is_caught(blueprint):
    blueprint["standing"]["pm"]["may_spawn"]["crew"].append("hotfix-swat")
    (problem,) = [p for p in problems_of(blueprint) if "hotfix-swat" in p]
    assert "feature-delivery" in problem


def test_a_crew_naming_an_unknown_role_is_caught(blueprint):
    blueprint["crews"]["feature-review"]["roles"] = ["qa"]
    assert any("does not match the catalog" in p for p in problems_of(blueprint))


def test_a_tool_scope_for_an_unknown_role_is_caught(blueprint):
    blueprint["tools"]["qa"] = {"allow": ["git.read"]}
    (problem,) = [p for p in problems_of(blueprint) if "tools.qa" in p]
    assert "role catalog" in problem


def test_company_role_cannot_hold_the_platform_merge_tool(blueprint):
    """git.merge_pr is the orchestrator's after the gate. A blueprint
    that could grant it would make the human merge gate optional."""
    blueprint["tools"]["pm"]["allow"].append("git.merge_pr")
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_no_role_scope_can_hold_the_platform_merge_tool(blueprint):
    blueprint["tools"]["reviewer"]["allow"].append("git.merge_pr")
    assert any("platform identity" in p for p in problems_of(blueprint))


def test_unknown_crew_name_is_caught(blueprint):
    """A crews: key outside the catalog is a document error, not something
    left for the compiler to resolve against nothing."""
    blueprint["crews"]["hotfix-swat"] = copy.deepcopy(
        blueprint["crews"]["feature-review"]
    )
    (problem,) = [p for p in problems_of(blueprint) if p.startswith("crews.hotfix-swat:")]
    assert "not a known crew" in problem
    assert "feature-delivery" in problem  # the known values are shown


def test_a_crew_must_list_exactly_the_catalog_roles(blueprint):
    """The catalog defines who is in a crew; a document adding a role, or
    swapping in a different (but existing) one, is wrong about the crew,
    not hiring."""
    blueprint["crews"]["feature-review"]["roles"] = ["reviewer", "implementer"]
    (problem,) = [p for p in problems_of(blueprint) if "does not match the catalog" in p]
    assert "'reviewer'" in problem
    blueprint["crews"]["feature-review"]["roles"] = ["implementer"]
    assert any("does not match the catalog" in p for p in problems_of(blueprint))


def test_a_tool_scope_cannot_widen_the_catalog(blueprint):
    """The reviewer-cannot-deliver contract is a catalog property: a
    blueprint granting the reviewer `git.open_pr` must fail validation,
    not compile into a wider bundle."""
    blueprint["tools"]["reviewer"]["allow"].append("git.open_pr")
    (problem,) = [p for p in problems_of(blueprint) if "git.open_pr" in p]
    assert "never widens" in problem
    assert "sandbox.read_file" in problem  # the allowed values are shown


def test_a_tool_scope_may_narrow_the_catalog(blueprint):
    blueprint["tools"]["implementer"] = {
        "allow": ["sandbox.read_file", "sandbox.run_tests"]
    }
    assert validate_blueprint(blueprint) is blueprint


# -- the lean-standing profile validates --------------------------------------------


def test_lean_roster_profile_validates(blueprint):
    """The checked-in document IS the lean profile; the rule that rejects
    standing implementers is a compile error, not a schema one — so a
    standing-implementer document still *validates* structurally."""
    assert validate_blueprint(blueprint) is blueprint
    standing_implementer = copy.deepcopy(blueprint)
    standing_implementer["standing"]["implementer"] = {
        "role": "implementer",
        "count": 1,
    }
    assert validate_blueprint(standing_implementer) is standing_implementer


# -- the merge gate --------------------------------------------------------


def delegated_merge() -> dict:
    return {
        "resolved_by": "policy",
        "delegate": {"paths": ["src/", "tests/"], "max_reworks": 2},
    }


def test_policy_resolved_merge_validates(blueprint):
    blueprint["workflows"]["review"]["gates"]["merge"] = delegated_merge()
    validate_blueprint(blueprint)


def test_a_gate_must_be_an_object_naming_its_resolver(blueprint):
    """A gate is an object naming its resolver; a bare string does not
    validate anywhere."""
    blueprint["workflows"]["review"]["gates"]["merge"] = "human_approval"
    assert problems_of(blueprint)


def test_an_undeclared_merge_gate_is_legal(blueprint):
    """No gate declared means the review ends without merging — the shape
    validates; what it means is the workflow's business."""
    del blueprint["workflows"]["review"]["gates"]
    validate_blueprint(blueprint)


def test_policy_without_a_delegation_is_refused(blueprint):
    blueprint["workflows"]["review"]["gates"]["merge"] = {"resolved_by": "policy"}
    (problem,) = [p for p in problems_of(blueprint) if "delegate" in p]
    assert "authority nobody wrote down" in problem


def test_delegation_under_a_human_gate_is_dead_configuration(blueprint):
    gate = delegated_merge()
    gate["resolved_by"] = "human"
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    (problem,) = [p for p in problems_of(blueprint) if "dead configuration" in p]
    assert "delegate" in problem


def test_delegation_declares_every_bound_or_nothing(blueprint):
    """Explicit paths and an explicit rework bound, the heartbeat stance: a
    defaulted bound is authority nobody wrote down."""
    for field in ("paths", "max_reworks"):
        document = copy.deepcopy(blueprint)
        gate = delegated_merge()
        del gate["delegate"][field]
        document["workflows"]["review"]["gates"]["merge"] = gate
        assert any(field in p for p in problems_of(document))


def test_repo_automation_can_never_be_delegated(blueprint):
    """A merged PR must not be able to rewrite the surface that governs
    merging — `.github/` is refused whatever else the delegation says."""
    gate = delegated_merge()
    gate["delegate"]["paths"].append(".github/")
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("can never be delegated" in p for p in problems_of(blueprint))


def test_a_broad_prefix_containing_a_carveout_is_refused(blueprint):
    """Overlap goes both ways: delegating a prefix that contains `.github/`
    would put the carve-out inside the delegated class."""
    gate = delegated_merge()
    gate["delegate"]["paths"] = ["."]
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("can never be delegated" in p for p in problems_of(blueprint))


def test_a_deny_path_can_never_be_delegated(blueprint):
    """Delegating a path no PR may touch is contradictory configuration,
    and dead configuration is an error."""
    blueprint["policies"]["deny_paths"] = [".github/workflows/", "docs/secrets/"]
    gate = delegated_merge()
    gate["delegate"]["paths"] = ["docs/"]
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("deny_paths" in p for p in problems_of(blueprint))


# -- the contract tree -------------------------------------------------------


def contract_tests_section() -> dict:
    return {"paths": ["tests/contract/"], "roles": ["contract-author"]}


def test_a_declared_contract_tree_validates(blueprint):
    blueprint["policies"]["contract_tests"] = contract_tests_section()
    validate_blueprint(blueprint)


def test_the_contract_tree_can_never_be_delegated(blueprint):
    """The oracle never merges under the delegation it certifies — refused
    at validate time, whatever else the delegated class says."""
    blueprint["policies"]["contract_tests"] = contract_tests_section()
    gate = delegated_merge()
    gate["delegate"]["paths"] = ["src/", "tests/contract/api/"]
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("contract_tests" in p and "oracle" in p for p in problems_of(blueprint))


def test_a_prefix_containing_the_contract_tree_is_refused(blueprint):
    """Overlap goes both ways, the `.github/` posture: delegating `tests/`
    would put the contract tree inside the delegated class."""
    blueprint["policies"]["contract_tests"] = contract_tests_section()
    blueprint["workflows"]["review"]["gates"]["merge"] = delegated_merge()
    assert any("contract_tests" in p for p in problems_of(blueprint))


def test_a_contract_tree_under_a_deny_path_is_refused(blueprint):
    """A contract path no PR may touch is a spec nobody could ever
    deliver — frozen by configuration."""
    blueprint["policies"]["deny_paths"] = [".github/workflows/", "specs/"]
    blueprint["policies"]["contract_tests"] = {
        "paths": ["specs/contract/"],
        "roles": ["reviewer"],
    }
    assert any("no role could ever deliver" in p for p in problems_of(blueprint))


def test_contract_owners_name_catalog_templates(blueprint):
    blueprint["policies"]["contract_tests"] = {
        "paths": ["tests/contract/"],
        "roles": ["spec-author"],
    }
    assert any(
        "contract_tests.roles" in p and "role catalog" in p
        for p in problems_of(blueprint)
    )


def test_the_contract_tree_declares_paths_and_owners_or_nothing(blueprint):
    for field in ("paths", "roles"):
        document = copy.deepcopy(blueprint)
        document["policies"]["contract_tests"] = contract_tests_section()
        del document["policies"]["contract_tests"][field]
        assert any(field in p for p in problems_of(document))


def test_intake_gate_names_its_resolver(blueprint):
    blueprint["workflows"]["intake"]["gates"] = {"intake": {"resolved_by": "human"}}
    validate_blueprint(blueprint)


def test_intake_gate_cannot_be_policy_resolved(blueprint):
    """Policy cannot judge whether a goal is worth doing; the schema names
    the only resolver intake has."""
    blueprint["workflows"]["intake"]["gates"] = {"intake": {"resolved_by": "policy"}}
    assert any("resolved_by" in p and "policy" in p for p in problems_of(blueprint))


def test_a_merge_gate_on_intake_is_not_writable(blueprint):
    """Each workflow names only the gates it actually parks on."""
    blueprint["workflows"]["intake"]["gates"] = {"merge": {"resolved_by": "human"}}
    assert any("merge" in p for p in problems_of(blueprint))


# -- the heartbeat -------------------------------------------------


def heartbeat_section() -> dict:
    return {
        "conductor": "pm",
        "reconcile_minutes": 30,
        "max_concurrent_runs": 1,
        "max_attempts": 2,
        "may_start": ["implement", "review"],
        "daily_cap": {"usd": 20},
    }


def test_heartbeat_section_validates(blueprint):
    blueprint["heartbeat"] = heartbeat_section()
    validate_blueprint(blueprint)


def test_heartbeat_is_optional(blueprint):
    assert "heartbeat" not in blueprint  # the lean example stays human-started
    validate_blueprint(blueprint)


def test_heartbeat_declares_every_cap_or_nothing(blueprint):
    """A defaulted cap is authority nobody wrote down: once the section
    exists, every field is required."""
    for field in heartbeat_section():
        document = copy.deepcopy(blueprint)
        document["heartbeat"] = heartbeat_section()
        del document["heartbeat"][field]
        assert any("heartbeat" in p and field in p for p in problems_of(document))


def test_heartbeat_attempts_start_at_one(blueprint):
    """`max_attempts: 1` is the one-shot company (every run is the only
    try); zero would mean the heartbeat may start nothing, which is what
    omitting the section says."""
    blueprint["heartbeat"] = heartbeat_section()
    blueprint["heartbeat"]["max_attempts"] = 0
    assert any("max_attempts" in p for p in problems_of(blueprint))
    blueprint["heartbeat"]["max_attempts"] = 1
    validate_blueprint(blueprint)


def test_heartbeat_may_not_start_intake(blueprint):
    """Goals enter through a human; the heartbeat may never feed itself
    new goals."""
    blueprint["heartbeat"] = heartbeat_section()
    blueprint["heartbeat"]["may_start"] = ["intake"]
    (problem,) = [p for p in problems_of(blueprint) if "intake" in p]
    assert "heartbeat.may_start" in problem
    assert "'implement'" in problem  # the allowed values are shown


def test_heartbeat_typo_is_rejected_not_ignored(blueprint):
    blueprint["heartbeat"] = heartbeat_section()
    blueprint["heartbeat"]["heartbeat_mins"] = 5
    del blueprint["heartbeat"]["reconcile_minutes"]
    problems = problems_of(blueprint)
    assert any("heartbeat_mins" in p and "spelling" in p for p in problems)
