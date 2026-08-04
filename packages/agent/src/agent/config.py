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
    # Which role this brain host serves (the operator sets it from the
    # instance spec). `pm` dispatches run the PM loop (plan step 17);
    # everything else runs the engineer graph.
    role_template: str = "backend-engineer"
    # The pod's identity, one of two shapes: standing hires mount a
    # kubelet-rotated projected token file (plan step 11); ephemeral crew
    # brains receive their minted short-TTL §2b token as GATEWAY_TOKEN at
    # provision time (plan step 16c) — env, not file, because it is minted
    # per run and revoked by registry row, never rotated.
    gateway_token_file: str = ""
    gateway_token: str = ""
    target_repo: str
    max_iterations: int = 10

    def resolve_gateway_token(self) -> str:
        # File first: the kubelet keeps it fresh; a task never outlives the
        # freshly read token's validity window.
        if self.gateway_token_file:
            return Path(self.gateway_token_file).read_text().strip()
        if self.gateway_token:
            return self.gateway_token
        raise RuntimeError(
            "no gateway credential: set GATEWAY_TOKEN_FILE (projected token) "
            "or GATEWAY_TOKEN (minted ephemeral identity)"
        )
