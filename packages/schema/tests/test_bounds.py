"""The bounds: the applied charter as every dispatch says it.

The text is read by models and asserted on by humans, so its shape is
pinned here line by line: one line per section, in a fixed order, facts
only, and an absent fact says so."""

from schema.bounds import NONE_DECLARED, render_bounds
from schema.gates import UNDELEGATABLE_PATHS

REPO = {"url": "github.com/example-org/hello-saas", "base_ref": "main"}
DOCUMENTS = {"strategy": "docs/STRATEGY.md", "architecture": "docs/ARCHITECTURE.md"}
POLICIES = {
    "branch_protection": {
        "allowed_pattern": "^feature/[a-z0-9-]+$",
        "protected": ["main"],
    },
    "deny_paths": [".github/workflows/"],
}
DELEGATED_GATE = {
    "resolved_by": "policy",
    "deadline_minutes": 1440,
    "delegate": {"paths": ["src/", "tests/unit/", "docs/"], "max_reworks": 1},
}
HUMAN_GATE = {"resolved_by": "human", "deadline_minutes": 1440}


def spec(policies: dict | None = POLICIES, gate: dict | None = HUMAN_GATE) -> dict:
    """A Company CR spec, as the registry row carries it."""
    review = {"name": "review", "crew": "feature-review", "gates": {}}
    if gate is not None:
        review["gates"] = {"merge": gate}
    return {
        "repo": REPO,
        "documents": DOCUMENTS,
        "policies": policies or {},
        "workflows": [{"name": "implement", "crew": "feature-delivery"}, review],
    }


def test_the_delegated_shape_line_by_line():
    assert render_bounds(spec(gate=DELEGATED_GATE)).splitlines() == [
        "Repository: github.com/example-org/hello-saas, base ref main.",
        "Branches: main is protected; a pull request's branch must match "
        "^feature/[a-z0-9-]+$.",
        "Paths no pull request may change: .github/workflows/.",
        "Contract tree: none declared.",
        "Documents: docs/STRATEGY.md is the strategy and docs/ARCHITECTURE.md the "
        "architecture masterplan; only the steward delivers into them, and the "
        "strategy is written once.",
        "Merge: policy merges a pull request without a human when the review "
        "passed, the platform's checks are green and every changed path is under "
        "docs/, src/ or tests/unit/; a pull request changing any other path, or "
        "anything under .github/, waits for a human. A failed review or a red "
        "check files a rework ticket, at most 1; after that the pull request "
        "waits for a human.",
    ]


def test_the_human_gate_shape_line_by_line():
    assert render_bounds(spec()).splitlines() == [
        "Repository: github.com/example-org/hello-saas, base ref main.",
        "Branches: main is protected; a pull request's branch must match "
        "^feature/[a-z0-9-]+$.",
        "Paths no pull request may change: .github/workflows/.",
        "Contract tree: none declared.",
        "Documents: docs/STRATEGY.md is the strategy and docs/ARCHITECTURE.md the "
        "architecture masterplan; only the steward delivers into them, and the "
        "strategy is written once.",
        "Merge: every pull request waits for a human's verdict.",
    ]


def test_a_contract_tree_names_its_writers_and_the_delegated_merge_fact():
    """The tree, who may deliver into it, the human wait on a change to
    it — and, only under a delegated gate, that a merge needs the base-ref
    tree green against the change."""
    policies = {
        **POLICIES,
        "contract_tests": {"paths": ["tests/contract/"], "roles": ["contract-author"]},
    }
    delegated = render_bounds(spec(policies, DELEGATED_GATE)).splitlines()[3]
    assert delegated == (
        "Contract tree: tests/contract/, written only by contract-author; a change "
        "to it waits for a human, and a merge is delegated only when the tree as "
        "it stands on the base ref is green against the change."
    )
    human = render_bounds(spec(policies, HUMAN_GATE)).splitlines()[3]
    assert human == (
        "Contract tree: tests/contract/, written only by contract-author; a change "
        "to it waits for a human."
    )


def test_no_merge_gate_means_a_review_ends_without_merging():
    assert render_bounds(spec(gate=None)).splitlines()[-1] == (
        "Merge: no merge gate is declared; a review ends without merging."
    )


def test_absent_facts_say_so_instead_of_dropping_their_line():
    """A company with no policies section still gets every line, so a
    reader finds the same fact in the same place for every company."""
    lines = render_bounds(spec(policies=None)).splitlines()
    assert len(lines) == 6
    assert lines[1] == f"Branches: {NONE_DECLARED}."
    assert lines[2] == f"Paths no pull request may change: {NONE_DECLARED}."
    assert lines[3] == f"Contract tree: {NONE_DECLARED}."


def test_a_covered_carve_out_prefix_is_not_repeated():
    """The merge line names the prefixes no delegation covers: the
    undelegatable surface and the deny paths, minus any prefix another
    already covers — `.github/workflows/` says nothing `.github/` did not,
    while a deny path elsewhere is its own fact."""
    assert UNDELEGATABLE_PATHS == (".github/",)
    policies = {**POLICIES, "deny_paths": [".github/workflows/", "infra/"]}
    merge = render_bounds(spec(policies, DELEGATED_GATE)).splitlines()[-1]
    assert "anything under .github/ or infra/, waits for a human" in merge
    assert ".github/workflows/" not in merge


def test_no_reworks_sends_a_failed_review_straight_to_a_human():
    gate = {**DELEGATED_GATE, "delegate": {"paths": ["src/"], "max_reworks": 0}}
    merge = render_bounds(spec(gate=gate)).splitlines()[-1]
    assert merge.endswith(
        "A failed review or a red check sends the pull request to a human."
    )
    assert "every changed path is under src/;" in merge


def test_branches_render_each_fact_on_its_own():
    only_protected = {"branch_protection": {"protected": ["release", "main"]}}
    assert render_bounds(spec(only_protected)).splitlines()[1] == (
        "Branches: main and release are protected."
    )
    only_pattern = {"branch_protection": {"allowed_pattern": "^fix/.*$"}}
    assert render_bounds(spec(only_pattern)).splitlines()[1] == (
        "Branches: a pull request's branch must match ^fix/.*$."
    )
