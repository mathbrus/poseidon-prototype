"""Preview: standing_name, fire and reorg are blueprint diffs, and you can read them
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
                agent_id=f"{standing_name}-1",
                company="hello-saas",
                standing_name=standing_name,
                role=role,
            )
            for standing_name, role in hires
        )
    )


def test_first_apply_creates_the_whole_roster(blueprint):
    diff = diff_plan(compile_blueprint(blueprint), EMPTY)
    assert {(c.agent_id, c.action) for c in diff.changes} == {("pm-1", "create")}


def test_converged_company_shows_no_changes(blueprint):
    snapshot = live(("pm", "pm"))
    diff = diff_plan(compile_blueprint(blueprint, snapshot=snapshot), snapshot)
    assert diff.empty
    assert "no changes" in diff.as_text()


def test_a_small_edit_shows_a_minimal_diff(blueprint):
    """Add one hire, see one line."""
    snapshot = live(("pm", "pm"))
    blueprint["standing"]["lead"] = {
        "role": "pm",
        "count": 1,
        "may_spawn": {"crew": ["feature-delivery"], "max_concurrent": 1},
    }
    diff = diff_plan(compile_blueprint(blueprint, snapshot=snapshot), snapshot)
    assert len(diff.changes) == 1
    (change,) = diff.changes
    assert change.agent_id == "lead-1" and change.action == "create"
    assert change.details == ("role pm",)


def test_the_firing_diff_is_a_deletion(blueprint):
    """Firing a standing agent is a blueprint edit: a live hire the
    blueprint no longer names is deleted, and nothing else changes."""
    snapshot = live(("pm", "pm"), ("lead", "pm"))
    diff = diff_plan(compile_blueprint(blueprint, snapshot=snapshot), snapshot)
    assert [(c.agent_id, c.action, c.details) for c in diff.changes] == [
        ("lead-1", "delete", ("no longer hired",))
    ]


def test_existing_agent_ids_are_preserved(blueprint):
    """Identity continuity across applies: a converged instance keeps the
    id policy and audit already know it by."""
    snapshot = RegistrySnapshot(
        instances=(
            InstanceSnapshot(
                agent_id="pm-original",
                company="hello-saas",
                standing_name="pm",
                role="pm",
            ),
        )
    )
    plan = compile_blueprint(blueprint, snapshot=snapshot)
    pm = next(i for i in plan.instances if i.standing_name == "pm")
    assert pm.agent_id == "pm-original"


def test_preview_reports_the_run_cap(blueprint):
    diff = diff_plan(compile_blueprint(blueprint), EMPTY)
    assert diff.per_run_usd == 5
    assert "$5/run" in diff.as_text()
