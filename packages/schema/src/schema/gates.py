"""The merge gate's delegable surface.

`gates.merge.resolved_by: policy` hands the merge decision to compiled
policy — the one grant that lets the company finish its own delivery.
Some surfaces must never sit inside that grant: a change to the
repository's own automation (`.github/`) is a change to what runs with
the repository's authority, so delegating it would let a merged PR
rewrite the rules that govern merging. The carve-out is a property of
the shape, refused at validate time and compiled into the bundle as a
deny — never a convention.

Shared by `schema.validate` (the document pass) and `compiler.checks`
(the compiler accepts unvalidated dicts and must fail closed on the same
problems) — the platform-only tool rule's arrangement.
"""

from typing import Any

# Prefixes no blueprint may delegate, whatever else it says. Everything
# under them is merge-gate governance itself or the repository's own
# automation; the compiler also unions in the document's `deny_paths`.
UNDELEGATABLE_PATHS = (".github/",)


def _overlaps(a: str, b: str) -> bool:
    """Prefix overlap in either direction: delegating `.github/workflows/`
    is as wrong as delegating a prefix that contains `.github/`."""
    return a.startswith(b) or b.startswith(a)


def merge_gate_problems(document: Any) -> list[str]:
    """Document-level merge-gate coherence, tolerant of malformed values
    (the schema pass reports those): a policy resolver declares its class,
    a human resolver carries none, and no declared class may overlap an
    undelegatable surface or a path no PR may touch anyway."""
    if not isinstance(document, dict):
        return []
    problems = []
    deny_paths = [
        p
        for p in (document.get("policies") or {}).get("deny_paths") or []
        if isinstance(p, str)
    ]
    workflows = document.get("workflows")
    for name, wf in (workflows if isinstance(workflows, dict) else {}).items():
        if not isinstance(wf, dict):
            continue
        gates = wf.get("gates")
        gate = gates.get("merge") if isinstance(gates, dict) else None
        if not isinstance(gate, dict):
            continue
        where = f"workflows.{name}.gates.merge"
        resolved_by = gate.get("resolved_by")
        delegate = gate.get("delegate")
        if resolved_by == "policy" and not isinstance(delegate, dict):
            problems.append(
                f"{where}: resolved_by 'policy' needs a `delegate` section "
                f"declaring the paths it may merge — an undeclared class is "
                f"authority nobody wrote down"
            )
        if resolved_by != "policy" and delegate is not None:
            problems.append(
                f"{where}.delegate: only a policy-resolved gate reads a "
                f"delegation — under resolved_by {resolved_by!r} it is dead "
                f"configuration, and dead configuration is an error"
            )
        paths = delegate.get("paths") if isinstance(delegate, dict) else None
        for path in paths if isinstance(paths, list) else []:
            if not isinstance(path, str):
                continue
            for carved in UNDELEGATABLE_PATHS:
                if _overlaps(path, carved):
                    problems.append(
                        f"{where}.delegate.paths: {path!r} overlaps "
                        f"{carved!r}, which can never be delegated — a "
                        f"merged PR must not be able to rewrite the surface "
                        f"that governs merging"
                    )
            for denied in deny_paths:
                if _overlaps(path, denied):
                    problems.append(
                        f"{where}.delegate.paths: {path!r} overlaps "
                        f"policies.deny_paths entry {denied!r} — no PR may "
                        f"touch it, so the delegation could never merge "
                        f"anything there"
                    )
    return problems


__all__ = ["UNDELEGATABLE_PATHS", "merge_gate_problems"]
