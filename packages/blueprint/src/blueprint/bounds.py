"""The bounds: the blueprint's gates and limits as they bind one company's
work, said once.

Compiled policy enforces the blueprint at the gateway, and a denial names
the rule that fired; nothing there tells an agent what it may do before
it tries. The bounds are that text. The platform renders it from the
applied spec — the record the bundle was compiled from, so the text and
the rules can never disagree — and carries it on every dispatch into the
procedure's system prompt, beside the vision, where text is trusted.

Facts only, in a fixed order, one line per section, so a reader (a model
or a person) finds the same fact in the same place for every company; an
absent fact says so rather than leaving its line out.
"""

from typing import Any

from blueprint.catalog import STEWARD_ROLE
from blueprint.documents import DOCUMENT_LABELS
from blueprint.gates import UNDELEGATABLE_PATHS

NONE_DECLARED = "none declared"


def render_bounds(spec: dict[str, Any]) -> str:
    """The bounds of the company whose applied spec this is: the Company
    CR's `repo`, `documents`, `policies` and `workflows`."""
    policies = spec.get("policies") or {}
    gate = _merge_gate(spec.get("workflows") or [])
    delegated = gate is not None and gate.get("resolved_by") == "policy"
    return "\n".join(
        [
            f"Repository: {spec['repo']['url']}, base ref {spec['repo']['base_ref']}.",
            f"Branches: {_branches(policies.get('branch_protection') or {})}",
            "Paths no pull request may change: "
            f"{_listed(policies.get('deny_paths') or [])}.",
            f"Contract tree: {_contract_tree(policies.get('contract_tests'), delegated)}",
            f"Documents: {_documents(spec['documents'])}",
            f"Merge: {_merge(gate, policies)}",
        ]
    )


def _merge_gate(workflows: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one merge gate, from whichever workflow declares it — the
    compiler's own rule for which gate `merge.resolve` compiles from."""
    for config in workflows:
        gate = (config.get("gates") or {}).get("merge")
        if gate:
            return gate
    return None


def _branches(protection: dict[str, Any]) -> str:
    protected = sorted(protection.get("protected") or [])
    pattern = protection.get("allowed_pattern")
    facts = []
    if protected:
        verb = "is" if len(protected) == 1 else "are"
        facts.append(f"{_either(protected, 'and')} {verb} protected")
    if pattern:
        facts.append(f"a pull request's branch must match {pattern}")
    return "; ".join(facts) + "." if facts else f"{NONE_DECLARED}."


def _contract_tree(contract: dict[str, Any] | None, delegated: bool) -> str:
    if not contract:
        return f"{NONE_DECLARED}."
    paths = sorted(contract.get("paths") or [])
    roles = sorted(contract.get("roles") or [])
    text = (
        f"{_listed(paths)}, written only by {_either(roles, 'or')}; "
        "a change to it waits for a human"
    )
    if delegated:
        text += (
            ", and a merge is delegated only when the tree as it stands on the "
            "base ref is green against the change"
        )
    return text + "."


def _documents(documents: dict[str, str]) -> str:
    strategy, architecture = documents["strategy"], documents["architecture"]
    return (
        f"{strategy} is the {DOCUMENT_LABELS['strategy']} and {architecture} the "
        f"{DOCUMENT_LABELS['architecture']}; only the {STEWARD_ROLE} delivers into "
        "them, and the strategy is written once."
    )


def _merge(gate: dict[str, Any] | None, policies: dict[str, Any]) -> str:
    if gate is None:
        return "no merge gate is declared; a review ends without merging."
    if gate.get("resolved_by") != "policy":
        return "every pull request waits for a human's verdict."
    delegate = gate.get("delegate") or {}
    paths = sorted(delegate.get("paths") or [])
    reworks = int(delegate.get("max_reworks") or 0)
    after_failure = (
        f"files a rework ticket, at most {reworks}; after that the pull request "
        "waits for a human."
        if reworks
        else "sends the pull request to a human."
    )
    return (
        "policy merges a pull request without a human when the review passed, "
        "the platform's checks are green and every changed path is under "
        f"{_either(paths, 'or')}; a pull request changing any other path, or "
        f"anything under {_either(_carved(policies), 'or')}, waits for a human. "
        f"A failed review or a red check {after_failure}"
    )


def _carved(policies: dict[str, Any]) -> list[str]:
    """The prefixes no delegation covers, as the bundle unions them —
    `UNDELEGATABLE_PATHS` and the deny paths — with a prefix another one
    already covers left out: `.github/workflows/` says nothing `.github/`
    did not."""
    carved = sorted({*UNDELEGATABLE_PATHS, *(policies.get("deny_paths") or [])})
    return [
        path
        for path in carved
        if not any(path != other and path.startswith(other) for other in carved)
    ]


def _listed(items: list[str]) -> str:
    return ", ".join(sorted(items)) if items else NONE_DECLARED


def _either(items: list[str], conjunction: str) -> str:
    """`a`, `a or b`, `a, b or c` — prose for a short list."""
    if len(items) <= 1:
        return "".join(items)
    return f"{', '.join(items[:-1])} {conjunction} {items[-1]}"


__all__ = ["NONE_DECLARED", "render_bounds"]
