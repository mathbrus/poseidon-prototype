"""Blueprint validation: the shipped example validates, and representative
invalid documents fail with errors that say where and what to write instead.

Both properties matter, so the
invalid cases assert on message *content*, not just on the raising.
"""

import copy
from pathlib import Path

import pytest
import yaml

from blueprint import load_blueprint, validate_blueprint
from blueprint.catalog import ROLES
from blueprint.validate import ValidationError

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


def test_pm_holds_no_implementer_tools(blueprint):
    """Spawn authority is not permission to wield the crew's tools —
    a gateway deny, not prompt hope. The example narrows no PM scope, so
    the PM's tools are its catalog role's."""
    assert "pm" not in blueprint["tools"]
    pm_tools = set(ROLES["pm"].tools)
    assert not any(t.startswith("sandbox.") for t in pm_tools)
    assert "git.open_pr" not in pm_tools
    assert {
        "issues.create",
        "issues.read",
        "issues.comment",
        "issues.dismiss",
    } == pm_tools


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
    blueprint["tools"]["reviewer"] = {"allow": ["git.read", "issues.delete"]}
    (problem,) = [p for p in problems_of(blueprint) if "issues.delete" in p]
    assert "tools.reviewer.allow" in problem
    assert "issues.create" in problem  # the allowed values are shown


def test_a_hire_is_one_instance(blueprint):
    """The reconciler names one Deployment and one Service per standing
    name, so a hire is an instance or a growth-path placeholder — never a
    count."""
    blueprint["standing"]["pm"]["count"] = 2
    assert any("count" in p for p in problems_of(blueprint))


def test_the_toolchain_is_declared_not_defaulted(blueprint):
    del blueprint["stack"]["toolchain"]["test_command"]
    assert any("test_command" in p for p in problems_of(blueprint))


def test_an_empty_tool_scope_is_rejected(blueprint):
    blueprint["tools"]["reviewer"] = {"allow": []}
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


def test_a_tool_scope_for_an_unknown_role_is_caught(blueprint):
    blueprint["tools"]["qa"] = {"allow": ["git.read"]}
    (problem,) = [p for p in problems_of(blueprint) if "tools.qa" in p]
    assert "role catalog" in problem


def test_company_role_cannot_hold_the_platform_merge_tool(blueprint):
    """git.merge_pr is the orchestrator's after the gate. A blueprint
    that could grant it would make the human merge gate optional."""
    blueprint["tools"]["reviewer"] = {"allow": ["git.read", "git.merge_pr"]}
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_no_role_scope_can_hold_the_repository_purge(blueprint):
    """`repo.purge` resets the repository when a person deletes the
    company: the console calls it, and no blueprint can hand it on."""
    blueprint["tools"]["pm"] = {"allow": ["issues.read", "repo.purge"]}
    (problem,) = [p for p in problems_of(blueprint) if "repo.purge" in p]
    assert "only by the console" in problem


def test_a_tool_scope_cannot_widen_the_catalog(blueprint):
    """The reviewer-cannot-deliver contract is a catalog property: a
    blueprint granting the reviewer `git.open_pr` must fail validation,
    not compile into a wider bundle."""
    blueprint["tools"]["reviewer"] = {"allow": ["git.read", "git.open_pr"]}
    (problem,) = [p for p in problems_of(blueprint) if "git.open_pr" in p]
    assert "never widens" in problem
    assert "sandbox.read_file" in problem  # the allowed values are shown


def test_a_tool_scope_may_narrow_the_catalog(blueprint):
    blueprint["tools"]["implementer"] = {
        "allow": ["sandbox.read_file", "sandbox.run_tests"]
    }
    assert validate_blueprint(blueprint) is blueprint


# -- the standing agents ------------------------------------------------------


def test_a_standing_implementer_still_validates(blueprint):
    """The example hires one standing agent, the PM; the rule that refuses
    a standing implementer is the compiler's, not the schema's — so a
    standing-implementer document still *validates* structurally."""
    assert validate_blueprint(blueprint) is blueprint
    standing_implementer = copy.deepcopy(blueprint)
    standing_implementer["standing"]["implementer"] = {
        "role": "implementer",
        "count": 1,
    }
    assert validate_blueprint(standing_implementer) is standing_implementer


# -- the merge gate --------------------------------------------------------


def automated_merge() -> dict:
    return {
        "deadline_minutes": 1440,
        "automate": {"paths": ["src/", "tests/"], "max_reworks": 2},
    }


def test_an_automated_merge_validates(blueprint):
    blueprint["workflows"]["review"]["gates"]["merge"] = automated_merge()
    validate_blueprint(blueprint)


def test_an_undeclared_merge_gate_is_legal(blueprint):
    """No gate declared means the review ends without merging — the shape
    validates; what it means is the workflow's business."""
    del blueprint["workflows"]["review"]["gates"]
    validate_blueprint(blueprint)


def test_automation_declares_every_bound_or_nothing(blueprint):
    """Explicit paths and an explicit rework bound, the heartbeat stance: a
    defaulted bound is authority nobody wrote down."""
    for field in ("paths", "max_reworks"):
        document = copy.deepcopy(blueprint)
        gate = automated_merge()
        del gate["automate"][field]
        document["workflows"]["review"]["gates"]["merge"] = gate
        assert any(field in p for p in problems_of(document))


def test_ci_configuration_can_never_be_automated(blueprint):
    """A merged PR must not be able to rewrite the surface that governs
    merging — `.github/` is refused whatever else the automation says."""
    gate = automated_merge()
    gate["automate"]["paths"].append(".github/")
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("never automated" in p for p in problems_of(blueprint))


def test_a_broad_prefix_containing_a_carveout_is_refused(blueprint):
    """Overlap goes both ways: automating a prefix that contains `.github/`
    would put the carve-out inside the automated paths."""
    gate = automated_merge()
    gate["automate"]["paths"] = ["."]
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("never automated" in p for p in problems_of(blueprint))


def test_a_deny_path_can_never_be_automated(blueprint):
    """Automating a path no PR may touch is contradictory configuration,
    and dead configuration is an error."""
    blueprint["policies"]["deny_paths"] = [".github/workflows/", "docs/secrets/"]
    gate = automated_merge()
    gate["automate"]["paths"] = ["docs/"]
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("deny_paths" in p for p in problems_of(blueprint))


# -- the contract tree -------------------------------------------------------


def contract_tests_section() -> dict:
    return {"paths": ["tests/contract/"], "roles": ["contract-author"]}


def test_a_declared_contract_tree_validates(blueprint):
    blueprint["policies"]["contract_tests"] = contract_tests_section()
    validate_blueprint(blueprint)


def test_the_contract_tree_can_never_be_automated(blueprint):
    """The contract tree never merges under the automation it grades —
    refused at validate time, whatever else the automated paths say."""
    blueprint["policies"]["contract_tests"] = contract_tests_section()
    gate = automated_merge()
    gate["automate"]["paths"] = ["src/", "tests/contract/api/"]
    blueprint["workflows"]["review"]["gates"]["merge"] = gate
    assert any("contract_tests" in p and "grades" in p for p in problems_of(blueprint))


def test_a_prefix_containing_the_contract_tree_is_refused(blueprint):
    """Overlap goes both ways, the `.github/` posture: automating `tests/`
    would put the contract tree inside the automated paths."""
    blueprint["policies"]["contract_tests"] = contract_tests_section()
    blueprint["workflows"]["review"]["gates"]["merge"] = automated_merge()
    assert any("contract_tests" in p for p in problems_of(blueprint))


def test_a_contract_tree_under_a_deny_path_is_refused(blueprint):
    """A contract path no PR may touch is a contract tree nobody could
    ever deliver into — fixed by configuration."""
    blueprint["policies"]["deny_paths"] = [".github/workflows/", "specs/"]
    blueprint["policies"]["contract_tests"] = {
        "paths": ["specs/contract/"],
        "roles": ["reviewer"],
    }
    assert any("no role could ever deliver" in p for p in problems_of(blueprint))


def test_contract_owners_name_catalog_roles(blueprint):
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


def test_a_workflow_names_no_conductor(blueprint):
    """The conductor is said once, at `heartbeat.conductor`; a workflow
    node carries only its crew, bounds and gates."""
    blueprint["workflows"]["implement"]["conductor"] = "pm"
    (problem,) = [p for p in problems_of(blueprint) if "conductor" in p]
    assert problem.startswith("workflows.implement:")


def test_a_workflow_names_its_crew(blueprint):
    del blueprint["workflows"]["implement"]["crew"]
    assert any("workflows.implement" in p and "crew" in p for p in problems_of(blueprint))


def test_scope_has_no_workflow_node(blueprint):
    """Scoping dispatches the conductor directly and configures nothing,
    so there is nothing to write under `workflows` for it."""
    blueprint["workflows"]["scope"] = {"crew": "feature-delivery"}
    assert any("workflows" in p and "scope" in p for p in problems_of(blueprint))


# -- the humans ------------------------------------------------------


def test_humans_are_required_and_named(blueprint):
    """Every company has a gate only a person can answer, so the blueprint
    names who — at least one, each a name, no name twice."""
    blueprint["company"]["humans"] = []
    assert any("humans" in p for p in problems_of(blueprint))
    blueprint["company"]["humans"] = ["owner", "owner"]
    assert any("humans" in p for p in problems_of(blueprint))
    blueprint["company"]["humans"] = ["Not A Name"]
    assert any("humans" in p for p in problems_of(blueprint))
    del blueprint["company"]["humans"]
    assert any("humans" in p for p in problems_of(blueprint))


# -- the documents -------------------------------------------------


def test_documents_are_required(blueprint):
    """The company writes its strategy and its architecture masterplan
    for itself; the blueprint names where, and names both."""
    del blueprint["company"]["documents"]["architecture"]
    assert any("architecture" in p for p in problems_of(blueprint))
    del blueprint["company"]["documents"]
    assert any("documents" in p for p in problems_of(blueprint))


def test_document_problems_name_a_missing_document():
    """The document pass names each absent document itself, so a caller
    that skips the schema pass (the compiler) fails closed on the same
    sentence."""
    from blueprint.gates import document_problems

    problems = document_problems({"company": {"documents": {"strategy": "docs/S.md"}}})
    assert problems == [
        "company.documents.architecture: required — a company names the path of "
        "both its documents"
    ]


def test_document_paths_are_distinct_and_inside_the_repository(blueprint):
    blueprint["company"]["documents"]["architecture"] = "docs/STRATEGY.md"
    assert any("distinct" in p for p in problems_of(blueprint))
    blueprint["company"]["documents"]["architecture"] = "../ARCHITECTURE.md"
    assert any("inside the repository" in p for p in problems_of(blueprint))
    blueprint["company"]["documents"]["architecture"] = "/etc/ARCHITECTURE.md"
    assert any("company.documents.architecture" in p for p in problems_of(blueprint))


def test_a_document_under_a_deny_path_is_refused(blueprint):
    """A path no PR may touch is a document nobody could ever write."""
    blueprint["policies"]["deny_paths"] = [".github/workflows/", "docs/"]
    assert any(
        "company.documents.strategy" in p and "deny_paths" in p
        for p in problems_of(blueprint)
    )


def test_a_document_inside_the_contract_tree_is_refused(blueprint):
    blueprint["policies"]["contract_tests"] = {
        "paths": ["docs/"],
        "roles": ["contract-author"],
    }
    assert any(
        "company.documents.strategy" in p and "contract_tests" in p
        for p in problems_of(blueprint)
    )


# -- the heartbeat -------------------------------------------------


def test_every_example_declares_a_heartbeat():
    """No shipped blueprint omits the section: a company always has one."""
    for example in sorted((REPO_ROOT / "examples").glob("*.yaml")):
        document = load_blueprint(example)
        assert "scope" in document["heartbeat"]["may_start"], example.name


def test_heartbeat_declares_every_cap_or_nothing(blueprint):
    """A defaulted cap is authority nobody wrote down: every field is
    required."""
    for field in list(blueprint["heartbeat"]):
        document = copy.deepcopy(blueprint)
        del document["heartbeat"][field]
        assert any("heartbeat" in p and field in p for p in problems_of(document))


def test_heartbeat_attempts_start_at_one(blueprint):
    """`max_attempts: 1` is the one-shot company (every run is the only
    try); zero would mean the heartbeat may start nothing, and a heartbeat
    that starts nothing is not a company."""
    blueprint["heartbeat"]["max_attempts"] = 0
    assert any("max_attempts" in p for p in problems_of(blueprint))
    blueprint["heartbeat"]["max_attempts"] = 1
    validate_blueprint(blueprint)


def test_open_ticket_cap_starts_at_one(blueprint):
    """Zero would refuse every scope before the first ticket exists."""
    blueprint["heartbeat"]["max_open_tickets"] = 0
    assert any("max_open_tickets" in p for p in problems_of(blueprint))


def test_heartbeat_may_start_only_the_three_workflows(blueprint):
    blueprint["heartbeat"]["may_start"] = ["deploy"]
    (problem,) = [p for p in problems_of(blueprint) if "deploy" in p]
    assert "heartbeat.may_start" in problem
    assert "'scope'" in problem  # the allowed values are shown
    blueprint["heartbeat"]["may_start"] = ["scope"]
    validate_blueprint(blueprint)
