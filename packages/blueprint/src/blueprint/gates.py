"""What a merge gate may automate.

A merge gate's `automate` block lets compiled policy merge without a
person's verdict — the one grant that lets the company finish its own
delivery. Some surfaces must never sit inside that grant: a change to the
repository's CI configuration (`.github/`) is a change to what runs with
the repository's authority, so automating it would let a merged PR
rewrite the rules that govern merging. The blueprint's declared contract
tree (`policies.contract_tests`) is the same shape one level up — the
tree that grades automated merges must not merge under the automation it
grades. The carve-out is a property of the shape, refused at validate
time and compiled into the bundle as a deny — never a convention.

Shared by `blueprint.validate` (the document pass) and `compiler.checks`
(the compiler accepts unvalidated dicts and must fail closed on the same
problems).
"""

from typing import Any

from blueprint.documents import DOCUMENT_NAMES

# Prefixes no blueprint may automate, whatever else it says. Everything
# under them is merge-gate governance itself or the repository's CI
# configuration; the compiler also unions in the document's `deny_paths`.
NEVER_AUTOMATED_PATHS = (".github/",)


def _overlaps(a: str, b: str) -> bool:
    """Prefix overlap in either direction: automating `.github/workflows/`
    is as wrong as automating a prefix that contains `.github/`."""
    return a.startswith(b) or b.startswith(a)


def _contract_paths(document: Any) -> list[str]:
    section = (document.get("policies") or {}).get("contract_tests")
    paths = section.get("paths") if isinstance(section, dict) else None
    return [p for p in paths if isinstance(p, str)] if isinstance(paths, list) else []


def merge_gate_problems(document: Any) -> list[str]:
    """Document-level merge-gate coherence, tolerant of malformed values
    (the schema pass reports those): no automated path may overlap a
    surface that is never automated or a path no PR may touch anyway."""
    if not isinstance(document, dict):
        return []
    problems = []
    deny_paths = [
        p
        for p in (document.get("policies") or {}).get("deny_paths") or []
        if isinstance(p, str)
    ]
    contract_paths = _contract_paths(document)
    workflows = document.get("workflows")
    for name, wf in (workflows if isinstance(workflows, dict) else {}).items():
        if not isinstance(wf, dict):
            continue
        gates = wf.get("gates")
        gate = gates.get("merge") if isinstance(gates, dict) else None
        if not isinstance(gate, dict):
            continue
        where = f"workflows.{name}.gates.merge.automate.paths"
        automate = gate.get("automate")
        paths = automate.get("paths") if isinstance(automate, dict) else None
        for path in paths if isinstance(paths, list) else []:
            if not isinstance(path, str):
                continue
            for carved in NEVER_AUTOMATED_PATHS:
                if _overlaps(path, carved):
                    problems.append(
                        f"{where}: {path!r} overlaps {carved!r}, which is "
                        f"never automated — a merged PR must not be able to "
                        f"rewrite the surface that governs merging"
                    )
            for denied in deny_paths:
                if _overlaps(path, denied):
                    problems.append(
                        f"{where}: {path!r} overlaps policies.deny_paths "
                        f"entry {denied!r} — no PR may touch it, so the "
                        f"automation could never merge anything there"
                    )
            for contract in contract_paths:
                if _overlaps(path, contract):
                    problems.append(
                        f"{where}: {path!r} overlaps policies.contract_tests "
                        f"entry {contract!r} — the contract tree grades "
                        f"automated merges, and it never merges under the "
                        f"automation it grades"
                    )
    return problems


def contract_tests_problems(document: Any) -> list[str]:
    """Document-level coherence of the declared contract tree, tolerant of
    malformed values like `merge_gate_problems`: a contract path under a
    deny path is a tree nobody could ever deliver into — the spec would be
    frozen by configuration."""
    if not isinstance(document, dict):
        return []
    problems = []
    deny_paths = [
        p
        for p in (document.get("policies") or {}).get("deny_paths") or []
        if isinstance(p, str)
    ]
    for path in _contract_paths(document):
        for denied in deny_paths:
            if _overlaps(path, denied):
                problems.append(
                    f"policies.contract_tests.paths: {path!r} overlaps "
                    f"policies.deny_paths entry {denied!r} — no PR may touch "
                    f"it, so no role could ever deliver the contract there"
                )
    return problems


def document_paths(document: Any) -> dict[str, str]:
    """The company's document paths by name, tolerant of malformed values."""
    section = (document.get("company") or {}).get("documents")
    return (
        {k: v for k, v in section.items() if isinstance(v, str)}
        if isinstance(section, dict)
        else {}
    )


def document_problems(document: Any) -> list[str]:
    """Document-level coherence of the company's document paths, the
    contract-tree posture: two documents cannot share a path, and a path
    no PR may touch is a document nobody could ever write."""
    if not isinstance(document, dict):
        return []
    problems = []
    paths = document_paths(document)
    for name in DOCUMENT_NAMES:
        if name not in paths:
            problems.append(
                f"company.documents.{name}: required — a company names the path "
                f"of both its documents"
            )
    if len(set(paths.values())) < len(paths):
        problems.append(
            "company.documents: the strategy and the architecture masterplan "
            "need distinct paths"
        )
    deny_paths = [
        p
        for p in (document.get("policies") or {}).get("deny_paths") or []
        if isinstance(p, str)
    ]
    contract_paths = _contract_paths(document)
    for name, path in paths.items():
        if ".." in path.split("/"):
            problems.append(
                f"company.documents.{name}: {path!r} must stay inside the repository"
            )
        for denied in deny_paths:
            if _overlaps(path, denied):
                problems.append(
                    f"company.documents.{name}: {path!r} overlaps policies.deny_paths "
                    f"entry {denied!r} — no PR may touch it, so the steward could "
                    f"never write the document"
                )
        for contract in contract_paths:
            if _overlaps(path, contract):
                problems.append(
                    f"company.documents.{name}: {path!r} overlaps policies.contract_tests "
                    f"entry {contract!r} — a document is not a test the company is "
                    f"graded by"
                )
    return problems


__all__ = [
    "NEVER_AUTOMATED_PATHS",
    "contract_tests_problems",
    "document_paths",
    "document_problems",
    "merge_gate_problems",
]
