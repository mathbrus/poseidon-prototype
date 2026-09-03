"""Agent settings: the env subset the agent host needs.

No secret ever reaches this container: the gateway credential is the
kubelet-rotated projected token file, the LLM provider key
lives in the llm-proxy, the git token in the gateway.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class AgentSettings(BaseSettings):
    mcp_gateway_url: str
    llm_proxy_url: str
    model: str
    # The catalog role this agent is: the pod spec sets it, and the host
    # refuses any procedure the role does not declare.
    role: str
    # The pod's identity: the kubelet-rotated projected token file its own
    # ServiceAccount mounts — one shape for standing agents and ephemeral
    # ephemeral agents alike. Crew revocation is a registry row termination,
    # checked by the gateway per call, not a property of the token.
    gateway_token_file: str = ""
    # The company's subject and purpose, from its applied blueprint via the
    # pod spec: the repository every tool call names, and the mission the
    # conductor's prompts carry.
    target_repo: str
    mission: str

    def resolve_gateway_token(self) -> str:
        # Read per call: the kubelet keeps the file fresh; a task never
        # outlives the freshly read token's validity window.
        if self.gateway_token_file:
            return Path(self.gateway_token_file).read_text().strip()
        raise RuntimeError(
            "no gateway credential: set GATEWAY_TOKEN_FILE to the projected "
            "service-account token path"
        )
