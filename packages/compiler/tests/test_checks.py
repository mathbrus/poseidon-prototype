"""Compile-time validations: each one is a governance mistake that must fail
here rather than at the first tool call in production."""

import pytest

from blueprint.catalog import ROLES
from compiler import CompileError, compile_blueprint


def problems_of(blueprint: dict) -> list[str]:
    with pytest.raises(CompileError) as caught:
        compile_blueprint(blueprint)
    return caught.value.problems


def test_hello_saas_compiles(blueprint):
    assert compile_blueprint(blueprint).company.name == "hello-saas"


def test_the_conductor_must_be_a_hired_spawner(blueprint):
    blueprint["standing"]["pm"]["count"] = 0
    (problem,) = [p for p in problems_of(blueprint) if "heartbeat.conductor" in p]
    assert "not an active standing spawner" in problem


def test_the_conductor_must_be_a_hire(blueprint):
    blueprint["heartbeat"]["conductor"] = "cto"
    assert any("cto" in p for p in problems_of(blueprint))


def test_unknown_tool_scope_fails_at_compile(blueprint):
    blueprint["tools"]["reviewer"] = {"allow": ["git.read", "issues.purge"]}
    assert any("issues.purge" in p for p in problems_of(blueprint))


def test_platform_tool_cannot_be_granted_to_a_role(blueprint):
    blueprint["tools"]["reviewer"] = {"allow": ["git.read", "git.merge_pr"]}
    (problem,) = [p for p in problems_of(blueprint) if "git.merge_pr" in p]
    assert "platform identity" in problem


def test_scope_for_a_role_nobody_uses_is_dead_config(blueprint):
    blueprint["tools"]["auditor"] = {"allow": ["issues.read"]}
    assert any("auditor" in p and "never apply" in p for p in problems_of(blueprint))


def test_unknown_role_is_a_compile_error(blueprint):
    """compile_blueprint accepts unvalidated dicts; an unknown role must be
    a CompileError problem, not a KeyError halfway through emission."""
    blueprint["standing"]["pm"]["role"] = "boss"
    assert any("role catalog" in p for p in problems_of(blueprint))


def test_spawn_authority_cannot_be_widened(blueprint):
    """Granting a role a crew its template does not permit is an error, not
    a silent drop — a workflow might depend on the grant. The reviewer role
    grants no spawn authority."""
    blueprint["standing"]["future-lead"] = {
        "role": "reviewer",
        "count": 0,
        "may_spawn": {"crew": ["feature-delivery"]},
    }
    (problem,) = [p for p in problems_of(blueprint) if "spawn authority" in p]
    assert "future-lead" in problem


def test_a_tool_scope_cannot_widen_the_catalog(blueprint):
    """Standing and ephemeral alike: a scope naming a tool the role's
    catalog entry does not grant is an error, never a silent intersection."""
    blueprint["tools"]["reviewer"] = {"allow": ["git.read", "git.open_pr"]}
    assert any("never widens" in p for p in problems_of(blueprint))
    del blueprint["tools"]["reviewer"]
    blueprint["tools"]["pm"] = {"allow": [*ROLES["pm"].tools, "sandbox.exec"]}
    assert any("never widens" in p for p in problems_of(blueprint))


def test_invalid_branch_pattern_fails_at_compile(blueprint):
    """A malformed allowed_pattern would otherwise surface as an OPA
    bundle-load failure long after apply."""
    blueprint["policies"]["branch_protection"]["allowed_pattern"] = "feature/("
    assert any("allowed_pattern" in p for p in problems_of(blueprint))


def test_a_workflow_crew_must_be_a_catalog_crew(blueprint):
    """The catalog defines crews; a workflow naming one it does not know is
    a compile error, not a bundle no fulfilment can ever match."""
    blueprint["workflows"]["implement"]["crew"] = "hotfix-swat"
    blueprint["standing"]["pm"]["may_spawn"]["crew"].append("hotfix-swat")
    assert any(
        "hotfix-swat" in p and "not a known crew" in p for p in problems_of(blueprint)
    )


def test_workflow_crew_must_be_spawnable_by_its_conductor(blueprint):
    """Otherwise the workflow compiles but can never be fulfilled."""
    blueprint["standing"]["pm"]["may_spawn"]["crew"] = ["feature-review", "stewardship"]
    (problem,) = [p for p in problems_of(blueprint) if "may not spawn" in p]
    assert "feature-delivery" in problem


def test_the_conductor_must_spawn_the_stewardship_crew(blueprint):
    """No workflow names the stewardship crew: the platform's bootstrap and
    architecture-request tickets route to it by trailer. A conductor that
    may not spawn it compiles a company that can never write its own
    documents, so the compiler refuses by name."""
    blueprint["standing"]["pm"]["may_spawn"]["crew"] = [
        "feature-delivery",
        "feature-review",
        "contract-authoring",
    ]
    (problem,) = [p for p in problems_of(blueprint) if "stewardship" in p]
    assert problem.startswith("heartbeat.conductor")


def test_compiler_fails_closed_on_a_missing_document(blueprint):
    """The compiler accepts unvalidated dicts: a document the blueprint does
    not place is a named problem, never a KeyError inside emission."""
    del blueprint["company"]["documents"]["architecture"]
    assert any(
        p.startswith("company.documents.architecture") for p in problems_of(blueprint)
    )
    del blueprint["company"]["documents"]
    assert any(p.startswith("company.documents.strategy") for p in problems_of(blueprint))


def test_a_tool_scope_needs_a_real_allow_list(blueprint):
    blueprint["tools"]["reviewer"] = {"allow": []}
    assert any("must not be empty" in p for p in problems_of(blueprint))


def test_runs_need_a_budget(blueprint):
    blueprint["budgets"]["per_run"]["usd"] = 0
    assert any("per_run" in p for p in problems_of(blueprint))


def test_child_scope_need_not_be_a_subset_of_the_conductor(blueprint):
    """The PM cannot call git.open_pr, yet the crew it spawns may.
    Dropping ⊆-parent is deliberate — spawn authority is not a
    capability ceiling — so this must compile."""
    plan = compile_blueprint(blueprint)
    pm = next(i for i in plan.instances if i.role == "pm")
    implementer = next(a for a in plan.ephemeral_agents if a.role == "implementer")
    assert "git.open_pr" in implementer.tools and "git.open_pr" not in pm.tools


def test_standing_implementer_is_a_compile_error(standing_implementer):
    """The strict lean-standing stance — no standing implementer counts;
    workflow nodes bind to spawners only. A standing-implementer shape is a
    compile error."""
    with pytest.raises(CompileError) as err:
        compile_blueprint(standing_implementer)
    assert "non-spawner role 'implementer'" in str(err.value)


def test_a_standing_steward_is_a_compile_error(blueprint):
    """The steward writes documents; it does not decide. Standing counts
    exist only for spawners, and it is not one."""
    blueprint["standing"]["archivist"] = {"role": "steward", "count": 1}
    (problem,) = [p for p in problems_of(blueprint) if "standing.archivist" in p]
    assert "non-spawner role 'steward'" in problem


def test_lean_roster_compiles_to_exactly_one_instance(blueprint):
    """The standing agents is the PM alone; execution is ephemeral crews."""
    plan = compile_blueprint(blueprint)
    assert [i.standing_name for i in plan.instances] == ["pm"]


# -- the heartbeat -------------------------------------------------


def test_a_company_without_a_heartbeat_does_not_compile(blueprint):
    """compile_blueprint accepts unvalidated dicts; a missing section is a
    named problem, not a KeyError — a company always has a heartbeat."""
    del blueprint["heartbeat"]
    (problem,) = [p for p in problems_of(blueprint) if p.startswith("heartbeat")]
    assert "required" in problem


def test_heartbeat_conductor_must_be_an_active_standing_spawner(blueprint):
    # reviewer is a real role but crew-only — never an active standing agent.
    blueprint["heartbeat"]["conductor"] = "reviewer"
    (problem,) = [p for p in problems_of(blueprint) if "heartbeat.conductor" in p]
    assert "not an active standing spawner" in problem


def test_heartbeat_daily_cap_must_fund_a_run(blueprint):
    """A daily cap below the per-run budget makes the heartbeat dead
    configuration — an error, like every other dead declaration."""
    blueprint["heartbeat"]["daily_cap"]["usd"] = 1
    (problem,) = [p for p in problems_of(blueprint) if "heartbeat.daily_cap" in p]
    assert "cannot fund a single run" in problem


# -- the merge automation ---------------------------------------------------


def test_automated_merge_compiles_the_gate_into_the_bundle(blueprint):
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "deadline_minutes": 1440,
        "automate": {"paths": ["src/", "tests/"], "max_reworks": 2},
    }
    plan = compile_blueprint(blueprint)
    assert "merge_gate.rego" in plan.bundle.files


def test_no_automation_compiles_no_automated_rule(blueprint):
    """Revocation is a blueprint diff: a gate without `automate` emits the
    human path of merge.resolve and no automated one, so an unattended
    merge falls to default deny."""
    gate_file = compile_blueprint(blueprint).bundle.files["merge_gate.rego"]
    assert "merge_resolve_human" in gate_file
    assert "merge_resolve_automated" not in gate_file


def test_compiler_fails_closed_on_a_carved_out_automation(blueprint):
    """`compile_blueprint` accepts unvalidated dicts — the carve-out that is
    never automated must refuse here too, not only in validate_blueprint."""
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "deadline_minutes": 1440,
        "automate": {"paths": [".github/"], "max_reworks": 2},
    }
    assert any("never automated" in p for p in problems_of(blueprint))


# -- the contract tree --------------------------------------------------------


def test_compiler_fails_closed_on_an_automated_contract_tree(blueprint):
    """The document-level refusal repeated at compile, the carve-out
    posture: the contract tree never merges under the automation it
    grades."""
    blueprint["policies"]["contract_tests"] = {
        "paths": ["tests/contract/"],
        "roles": ["reviewer"],
    }
    blueprint["workflows"]["review"]["gates"]["merge"] = {
        "deadline_minutes": 1440,
        "automate": {"paths": ["src/", "tests/"], "max_reworks": 2},
    }
    assert any("contract_tests" in p and "grades" in p for p in problems_of(blueprint))


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
        "roles": ["reviewer"],
    }
    assert any("no role could ever deliver" in p for p in problems_of(blueprint))
