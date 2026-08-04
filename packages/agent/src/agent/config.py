"""Agent settings: the C7 subset the backend-engineer agent needs.

No secret ever reaches this container (E3): the gateway credential is the
kubelet-rotated projected token file (plan step 11), the LLM provider key
lives in the llm-proxy, the git token in the gateway.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    mcp_gateway_url: str
    llm_proxy_url: str
    model: str
    # Projected identity token (plan step 11); the static bearer token is
    # the pre-step-11 fallback and is deleted at the step-12 cutover.
    gateway_token_file: str = ""
    agent_bearer_token: str = ""
    target_repo: str
    max_iterations: int = 10

    def gateway_token(self) -> str:
        # Read per task dispatch: the kubelet rotates the file, and a task
        # never outlives the freshly read token's validity window.
        if self.gateway_token_file:
            return Path(self.gateway_token_file).read_text().strip()
        if self.agent_bearer_token:
            return self.agent_bearer_token
        raise RuntimeError(
            "no gateway credential: set GATEWAY_TOKEN_FILE (projected token) "
            "or AGENT_BEARER_TOKEN (static, pre-step-11)"
        )
