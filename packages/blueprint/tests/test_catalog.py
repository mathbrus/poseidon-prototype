"""The versioned catalogs: the invariants the compiler and the gateway rely on.

These are contract tests, not coverage — each one is a rule another package
assumes it can rely on.
"""

from blueprint.catalog import CREW_TEMPLATES, PROCEDURES, ROLES, STEWARD_ROLE
from blueprint.tools import KNOWN_TOOLS, PLATFORM_ONLY, RATIONALE_TOOLS


def test_catalog_covers_the_mvp_roles():
    assert set(ROLES) == {"pm", "implementer", "reviewer", "contract-author", "steward"}


def test_every_role_scope_is_a_known_tool():
    for role in ROLES.values():
        assert set(role.tools) <= KNOWN_TOOLS, role.name


def test_no_role_carries_a_platform_only_tool():
    for role in ROLES.values():
        assert not set(role.tools) & PLATFORM_ONLY, role.name


def test_every_role_declares_known_procedures():
    """The agent host refuses a procedure its role does not declare; the
    declaration must name procedures the host actually has."""
    for role in ROLES.values():
        assert role.procedures, role.name
        assert set(role.procedures) <= set(PROCEDURES), role.name
    assert ROLES["pm"].procedures == ("scope", "decide")
    assert ROLES["implementer"].procedures == ("implement", "deliver")
    assert ROLES["reviewer"].procedures == ("review",)


def test_pm_spawns_but_does_not_execute():
    pm = ROLES["pm"]
    assert set(pm.may_spawn) == {
        "contract-authoring",
        "feature-delivery",
        "feature-review",
        "stewardship",
    }
    assert not any(t.startswith("sandbox.") for t in pm.tools)
    assert "git.open_pr" not in pm.tools


def test_crew_templates_are_the_catalog_crews():
    assert set(CREW_TEMPLATES) == {
        "contract-authoring",
        "feature-delivery",
        "feature-review",
        "stewardship",
    }


def test_every_crew_lists_catalog_roles_and_one_of_each():
    for crew in CREW_TEMPLATES.values():
        assert crew.roles, f"{crew.name} lists no role"
        assert set(crew.roles) <= set(ROLES), crew.name
        assert len(set(crew.roles)) == len(crew.roles), crew.name


def test_the_shipped_crews_are_one_role_each():
    assert CREW_TEMPLATES["feature-delivery"].roles == ("implementer",)
    assert CREW_TEMPLATES["feature-review"].roles == ("reviewer",)
    assert CREW_TEMPLATES["contract-authoring"].roles == ("contract-author",)
    assert CREW_TEMPLATES["stewardship"].roles == (STEWARD_ROLE,)


def test_implementer_can_deliver_and_reviewer_cannot():
    assert "git.open_pr" in ROLES["implementer"].tools
    assert "git.open_pr" not in ROLES["reviewer"].tools
    assert {"git.read", "git.comment"} <= set(ROLES["reviewer"].tools)


def test_contract_author_is_a_distinct_deliver_capable_role():
    """The spec's author delivers through the same governed `git.open_pr`
    as any change — deliver-capable unlike `reviewer`, and a distinct role
    so `policies.contract_tests` can fence the tree to it, crew-request
    rows never collide with a same-run delivery crew, and reviewer
    independence (keyed on crew_template) holds against it for free."""
    assert "git.open_pr" in ROLES["contract-author"].tools
    assert "contract-author" not in CREW_TEMPLATES["feature-delivery"].roles


def test_every_crew_member_may_ask_for_an_architecture_change():
    """Asking is not writing: the implementer, reviewer and contract
    author hold `architecture.request`; the steward writes the plan and
    the PM files tickets with its own tools."""
    for name in ("implementer", "reviewer", "contract-author"):
        assert "architecture.request" in ROLES[name].tools, name
    for name in ("pm", STEWARD_ROLE):
        assert "architecture.request" not in ROLES[name].tools, name


def test_the_steward_is_a_distinct_deliver_capable_role():
    """The company's documents have one writer: a role the compiled
    document fences can name without naming the implementer, delivering
    through the same governed `git.open_pr`, and only ever ephemeral."""
    steward = ROLES[STEWARD_ROLE]
    assert "git.open_pr" in steward.tools
    assert steward.procedures == ("implement", "deliver")
    assert not steward.may_spawn
    assert STEWARD_ROLE not in CREW_TEMPLATES["feature-delivery"].roles


def test_the_tools_that_owe_a_rationale_are_gateway_tools():
    assert RATIONALE_TOOLS <= KNOWN_TOOLS
