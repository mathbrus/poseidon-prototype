"""OpenShell evaluation backend (plan step 24, O-5) — strictly non-gating.

Nothing on the MVP critical path imports this package; `k8s_jobs` is the
default backend. The evaluation's findings live in
`markdowns/MVP/cleaned/step24-openshell-eval.md`.
"""

from sandbox.backends.openshell.backend import (
    PINNED_VERSION,
    ContractGap,
    OpenShellBackend,
    OpenShellUnavailable,
    build_spec,
    sandbox_name,
)
from sandbox.backends.openshell.ocsf import (
    CLASS_TO_TOOL,
    UnmappedOcsfClass,
    to_audit_event,
)

__all__ = [
    "CLASS_TO_TOOL",
    "ContractGap",
    "OpenShellBackend",
    "OpenShellUnavailable",
    "PINNED_VERSION",
    "UnmappedOcsfClass",
    "build_spec",
    "sandbox_name",
    "to_audit_event",
]
