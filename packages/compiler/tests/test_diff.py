"""Preview: hire, fire and reorg are blueprint diffs, and you can read them
before they converge."""

from compiler import (
    EMPTY,
    InstanceSnapshot,
    RegistrySnapshot,
    compile_blueprint,
    diff_plan,
)


def live(*hires: tuple[str, str, str]) -> RegistrySnapshot:
    return RegistrySnapshot(
        instances=tuple(
            InstanceSnapshot(
                agent_id=f"{hire}-1",
                company="hello-saas",
                hire=hire,
                role_template=template,
                model_tier=tier,
            )
            for hire, template, tier in hires
        )
    )


def test_first_apply_creates_the_whole_roster(blueprint):
    diff = diff_plan(compile_blueprint(blueprint), EMPTY)
    assert {(c.agent_id, c.action) for c in diff.changes} == {("pm-1", "create")}


def test_converged_company_shows_no_changes(blueprint):
    snapshot = live(("pm", "pm", "standard"))
    diff = diff_plan(compile_blueprint(blueprint, snapshot=snapshot), snapshot)
    assert diff.empty
    assert "no changes" in diff.as_text()


def test_a_small_edit_shows_a_minimal_diff(blueprint):
    """The done-when: change one field, see one line."""
    snapshot = live(("pm", "pm", "standard"))
    blueprint["departments"]["roles"]["pm"]["model_tier"] = "frontier"
    diff = diff_plan(compile_blueprint(blueprint, snapshot=snapshot), snapshot)
    assert len(diff.changes) == 1
    (change,) = diff.changes
    assert change.agent_id == "pm-1" and change.action == "update"
    assert change.details == ("model_tier standard -> frontier",)


def test_the_firing_diff_is_a_deletion(lean):
    """Firing a standing hire is a blueprint edit — the live demo of
    fire-as-diff."""
    snapshot = live(
        ("pm", "pm", "standard"), ("backend-engineer", "backend-engineer", "frontier")
    )
    diff = diff_plan(compile_blueprint(lean, snapshot=snapshot), snapshot)
    assert [(c.agent_id, c.action) for c in diff.changes] == [
        ("backend-engineer-1", "delete")
    ]


def test_count_growth_is_distinct_creates(blueprint):
    """count > 1 mints one agent_id per instance and previews the growth —
    a hire-keyed diff used to collapse it into 'no changes'."""
    snapshot = live(("pm", "pm", "standard"))
    blueprint["departments"]["roles"]["pm"]["count"] = 3
    plan = compile_blueprint(blueprint, snapshot=snapshot)
    pm_ids = [i.agent_id for i in plan.instances if i.hire == "pm"]
    assert len(pm_ids) == len(set(pm_ids)) == 3
    assert pm_ids[0] == "pm-1"  # the live instance keeps its id
    diff = diff_plan(plan, snapshot)
    creates = {c.agent_id for c in diff.changes if c.action == "create"}
    assert creates == set(pm_ids[1:])


def test_existing_agent_ids_are_preserved(blueprint):
    """Identity continuity across applies: a converged instance keeps the
    id policy and audit already know it by."""
    snapshot = RegistrySnapshot(
        instances=(
            InstanceSnapshot(
                agent_id="pm-original",
                company="hello-saas",
                hire="pm",
                role_template="pm",
                model_tier="standard",
            ),
        )
    )
    plan = compile_blueprint(blueprint, snapshot=snapshot)
    pm = next(i for i in plan.instances if i.hire == "pm")
    assert pm.agent_id == "pm-original"


def test_preview_projects_cost(blueprint):
    diff = diff_plan(compile_blueprint(blueprint), EMPTY)
    assert diff.per_run_usd == 5
    assert diff.projected_monthly_usd == 50  # the lean roster: pm alone
    assert "$5/run" in diff.as_text()
