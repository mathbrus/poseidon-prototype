"""Agent settings: the C7 subset the backend-engineer agent needs.

No secrets besides AGENT_BEARER_TOKEN ever reach this container (E3);
the LLM provider key lives in the llm-proxy, the git token in the gateway.
"""

from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    mcp_gateway_url: str
    llm_proxy_url: str
    model: str
    agent_bearer_token: str
    target_repo: str
    max_iterations: int = 10
