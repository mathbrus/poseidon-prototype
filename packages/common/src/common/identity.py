"""Caller identity: who a verified credential says is calling.

An `Identity` carries company, agent_id, lifecycle and role (the catalog
role, for every lifecycle) — plus what the verifier learned on the way:
the standing caller's namespace (for run-binding enrichment), and the
ephemeral caller's run binding, parentage and crew from its live
`run_participants` row.

Produced exclusively by `common.authn`, from a Kubernetes-issued token
verified through TokenReview. Two shapes present one: a workload's
projected service-account token (the two gateways verify these, against
the registry) and a human's token (the console verifies these, against
the humans the deployment declares). Policy keys on the lifecycle, so a
human and a workload can never be confused for one another.
"""

from pydantic import BaseModel

# The orchestrator's privileged identity (the `platform` lifecycle).
# Policy keys on the lifecycle; these names are what the audit story shows.
ORCHESTRATOR_ID = "orchestrator"
PLATFORM_ROLE = "platform"

LIFECYCLE_STANDING = "standing"
LIFECYCLE_EPHEMERAL = "ephemeral"
LIFECYCLE_PLATFORM = "platform"
# A verified person acting through the console. Not a catalog role: the
# catalog lists what agents may be, and a human is never an agent.
LIFECYCLE_HUMAN = "human"
HUMAN_ROLE = "human"
# The console itself, calling the MCP gateway to read the company's two
# documents for a person. Never the platform lifecycle, whose rules allow
# every platform tool, merge included: the company bundle allows this one
# exactly one read, scoped to its repository, base ref and document paths.
LIFECYCLE_CONSOLE = "console"
CONSOLE_ID = "console"
CONSOLE_ROLE = "console"


class Identity(BaseModel):
    agent_id: str
    role: str
    company: str | None = None
    lifecycle: str = LIFECYCLE_STANDING
    # Standing callers: the namespace their ServiceAccount (and pods) live
    # in — compared against a sandbox registration's namespace when
    # verifying a run binding.
    namespace: str | None = None
    # Ephemeral callers: the parentage and crew, read from the live
    # run_participants row at verification time.
    run_id: str | None = None
    parent_agent_id: str | None = None
    crew_template: str | None = None


def bearer_token(authorization: str | None) -> str | None:
    """The token out of an Authorization header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
