"""Caller identity (§4.9): who a verified credential says is calling.

Since plan step 11 an `Identity` names the four §5.1 fields — company,
agent_id, lifecycle, role — and is produced by `gateway.authn` from one of
three credentials: a projected Kubernetes service-account token (standing
agents, the orchestrator), a platform-signed ephemeral JWT (§2b, minted
from step 16a), or one of the two PoC static bearer tokens, which remain
verified until the step-12 cutover deletes them.
"""

from pydantic import BaseModel

from gateway.config import GatewaySettings

AGENT_ID = "backend-engineer-1"
AGENT_ROLE = "backend-engineer"
RUNNER_ID = "runner"
RUNNER_ROLE = "runner"

LIFECYCLE_STANDING = "standing"
LIFECYCLE_EPHEMERAL = "ephemeral"
LIFECYCLE_PLATFORM = "platform"


class Identity(BaseModel):
    agent_id: str
    role: str
    company: str | None = None
    lifecycle: str = LIFECYCLE_STANDING


def token_map(settings: GatewaySettings) -> dict[str, Identity]:
    """The static PoC placeholders (contract C7). The mapped identities
    mirror what the workload-token paths produce for the same callers, so
    policy and audit cannot tell the credential types apart before the
    static ones are deleted at plan step 12."""
    return {
        settings.agent_bearer_token: Identity(
            agent_id=AGENT_ID, role=AGENT_ROLE, lifecycle=LIFECYCLE_STANDING
        ),
        settings.runner_bearer_token: Identity(
            agent_id=RUNNER_ID, role=RUNNER_ROLE, lifecycle=LIFECYCLE_PLATFORM
        ),
    }


def bearer_token(authorization: str | None) -> str | None:
    """The token out of an Authorization header, or None."""
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return token.strip() or None
