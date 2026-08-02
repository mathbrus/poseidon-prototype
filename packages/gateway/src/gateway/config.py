"""Gateway settings: the C7 tokens it needs plus gateway-internal knobs."""

from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    agent_bearer_token: str
    runner_bearer_token: str
    opa_url: str = "http://opa:8181"
    # Git backend (C7: GIT_TOKEN/BASE_REF live in the gateway only).
    git_token: str | None = None
    base_ref: str = "main"
    github_api_url: str = "https://api.github.com"
