"""The acceptance suite's brain-of-record rule, apart from any cluster.

The suite dispatches real LLM calls; against a paid brain those bill a
provider, which is a spend decision no test run may make implicitly. The
rule is pure, so `scripts/acceptance.sh`'s preflight and the suite's
session guard (`tests/e2e_kind/conftest.py`) apply the same function to
the image the cluster deploys, and its edges are tested without one.
"""

from common.topology import PLATFORM_NAMESPACE

# The mock brain's image, matched by repository name exactly: a lookalike
# (`poseidon-mock-brain-x`, a LiteLLM tag that mentions it) is a paid brain.
MOCK_BRAIN_IMAGE = "poseidon-mock-brain"


def image_repository(image: str) -> str:
    """`registry:5000/name:tag@sha256:…` → `name`."""
    name = image.split("@", 1)[0].rsplit("/", 1)[-1]
    return name.split(":", 1)[0]


def paid_brain_problem(image: str, allow: str | None) -> str | None:
    """Why the session must not run, or None. Fail-safe defaults: an
    unreadable brain refuses too — the dangerous condition is the absent
    input."""
    if not image:
        return (
            "cannot read the deployed brain"
            f" (deployment/llm-proxy in {PLATFORM_NAMESPACE});"
            " refusing to run against an unverified brain —"
            " is the cluster up? deploy/kind/kind-up.sh"
        )
    if image_repository(image) == MOCK_BRAIN_IMAGE:
        return None
    if allow == "1":
        return None
    return (
        f"the cluster runs a PAID brain ({image}): e2e runs would bill a provider."
        " Redeploy on the mock (REAL_LLM=0 deploy/kind/kind-up.sh), or set"
        " POSEIDON_ALLOW_PAID=1 to spend deliberately."
    )
