"""Caller identity (§4.9): who a verified credential says is calling.

An `Identity` carries the §5.1 fields — company, agent_id, lifecycle, role —
plus what the verifier learned on the way: the standing caller's namespace
(for run-binding enrichment), and the ephemeral caller's run binding and
crew coordinates from its live `run_participants` row.

Produced exclusively by `common.authn` from a workload credential: a
projected Kubernetes service-account token (standing agents, the
orchestrator) or a platform-signed ephemeral JWT (§2b). Shared by both
gateways — the MCP gateway and the LLM gateway verify the same identities
(§2b). The PoC static bearer tokens died at the step-12 cutover.
"""

from pydantic import BaseModel

# The orchestrator's privileged identity (§5.1 `platform` lifecycle) — the
# successor of the PoC's "runner". Policy keys on the lifecycle; these names
# are what the audit story shows.
ORCHESTRATOR_ID = "orchestrator"
PLATFORM_ROLE = "platform"

LIFECYCLE_STANDING = "standing"
LIFECYCLE_EPHEMERAL = "ephemeral"
LIFECYCLE_PLATFORM = "platform"


class Identity(BaseModel):
    agent_id: str
    role: str
    company: str | None = None
    lifecycle: str = LIFECYCLE_STANDING
    # Standing callers: the namespace their ServiceAccount (and pods) live
    # in — compared against a sandbox registration's namespace when
    # verifying a run binding.
    namespace: str | None = None
    # Ephemeral callers: the §5.1 parentage and crew coordinates, read from
    # the live run_participants row at verification time.
    run_id: str | None = None
    parent_agent_id: str | None = None
    crew_template: str | None = None
    crew_role: str | None = None


def bearer_token(authorization: str | None) -> str | None:
    """The token out of an Authorization header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
