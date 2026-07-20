"""Static bearer-token -> identity map (contract C7)."""

from pydantic import BaseModel

from gateway.config import GatewaySettings

AGENT_ID = "backend-engineer-1"
AGENT_ROLE = "backend-engineer"
RUNNER_ID = "runner"
RUNNER_ROLE = "runner"


class Identity(BaseModel):
    agent_id: str
    role: str


def token_map(settings: GatewaySettings) -> dict[str, Identity]:
    return {
        settings.agent_bearer_token: Identity(agent_id=AGENT_ID, role=AGENT_ROLE),
        settings.runner_bearer_token: Identity(agent_id=RUNNER_ID, role=RUNNER_ROLE),
    }


def authenticate(authorization: str | None, tokens: dict[str, Identity]) -> Identity | None:
    if not authorization:
        return None
    scheme, _, token = authorization.partition(" ")
    if scheme.lower() != "bearer":
        return None
    return tokens.get(token.strip())
