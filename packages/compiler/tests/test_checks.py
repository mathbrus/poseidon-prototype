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


def test_scope_for_a_role_nobody_uses_is_dead_config(blueprint):
    blueprint["tools"]["auditor"] = {"allow": ["issues.read"]}
    assert any("auditor" in p and "never apply" in p for p in problems_of(blueprint))


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
    (problem,) = [
        p for p in problems_of(blueprint) if p.startswith("workflows.implement.crew")
    ]
    assert "'hotfix-swat' is not a known crew" in problem


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


def test_no_automation_compiles_no_automated_rule(blueprint):
    """Revocation is a blueprint diff: a gate without `automate` emits the
    human path of merge.resolve and no automated one, so an unattended
    merge falls to default deny."""
    gate_file = compile_blueprint(blueprint).bundle.files["merge_gate.rego"]
    assert "merge_resolve_human" in gate_file
    assert "merge_resolve_automated" not in gate_file


# -- the document's own checks ------------------------------------------------


def _automated(*paths: str) -> dict:
    return {
        "deadline_minutes": 1440,
        "automate": {"paths": list(paths), "max_reworks": 2},
    }


@pytest.mark.parametrize(
    ("edits", "where", "says"),
    [
        pytest.param(
            {"tools.reviewer": {"allow": ["git.read", "git.merge_pr"]}},
            "tools.reviewer.allow",
            "platform identity",
            id="a-platform-tool-granted",
        ),
        pytest.param(
            {"standing.pm.role": "boss"},
            "standing.pm.role",
            "role catalog",
            id="an-unknown-role",
        ),
        pytest.param(
            {"tools.reviewer": {"allow": ["git.read", "git.open_pr"]}},
            "tools.reviewer.allow",
            "never widens",
            id="a-widening-scope",
        ),
        pytest.param(
            {"tools.reviewer": {"allow": []}},
            "tools.reviewer.allow",
            "must not be empty",
            id="an-empty-scope",
        ),
        pytest.param(
            {"company.documents.architecture": None},
            "company.documents.architecture",
            "required",
            id="a-missing-document",
        ),
        pytest.param(
            {"company.documents": None},
            "company.documents.strategy",
            "required",
            id="no-documents",
        ),
        pytest.param(
            {"workflows.review.gates.merge": _automated(".github/")},
            "workflows.review.gates.merge.automate.paths",
            "never automated",
            id="an-automated-carve-out",
        ),
        pytest.param(
            {
                "policies.contract_tests": {
                    "paths": ["tests/contract/"],
                    "roles": ["reviewer"],
                },
                "workflows.review.gates.merge": _automated("src/", "tests/"),
            },
            "workflows.review.gates.merge.automate.paths",
            "grades",
            id="an-automated-contract-tree",
        ),
        pytest.param(
            {
                "policies.contract_tests": {
                    "paths": ["tests/contract/"],
                    "roles": ["spec-author"],
                }
            },
            "policies.contract_tests.roles",
            "role catalog",
            id="an-unknown-contract-owner",
        ),
        pytest.param(
            {
                "policies.deny_paths": [".github/workflows/", "specs/"],
                "policies.contract_tests": {
                    "paths": ["specs/contract/"],
                    "roles": ["reviewer"],
                },
            },
            "policies.contract_tests.paths",
            "no role could ever deliver",
            id="a-denied-contract-tree",
        ),
    ],
)
def test_the_compile_repeats_the_document_checks(blueprint, edits, where, says):
    """`compile_blueprint` accepts unvalidated dicts, so it runs the
    blueprint package's catalog, gate and document checks itself and fails
    closed on the same problems — a named problem, never a KeyError halfway
    through emission. What each problem says is the blueprint package's to
    prove; that the compile reports it is proven here. Each edit sets a
    dotted key, or deletes it when the value is None."""
    for dotted, value in edits.items():
        *parents, key = dotted.split(".")
        node = blueprint
        for parent in parents:
            node = node[parent]
        if value is None:
            del node[key]
        else:
            node[key] = value
    assert any(p.startswith(where) and says in p for p in problems_of(blueprint))
