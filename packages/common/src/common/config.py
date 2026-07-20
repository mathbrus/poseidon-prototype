"""Settings for the C7 environment variables.

Each service reads only the subset it needs, so every field is optional here;
services validate presence of what they require at startup.
"""

from pydantic_settings import BaseSettings


class Settings(BaseSettings):
    git_token: str | None = None
    llm_api_key: str | None = None
    target_repo: str | None = None
    base_ref: str = "main"
    model: str | None = None
    max_iterations: int = 10
    agent_bearer_token: str | None = None
    runner_bearer_token: str | None = None
    mcp_gateway_url: str | None = None
    llm_proxy_url: str | None = None
