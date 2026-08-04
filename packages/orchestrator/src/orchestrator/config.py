"""Orchestrator settings.

Split in two: `TemporalSettings` is all a Temporal client needs (the CLI —
every field has a default suitable for a port-forwarded kind cluster);
`WorkerSettings` adds what activities need. The worker's gateway credential
is its projected service-account token (plan step 11); GIT_TOKEN and
provider keys never appear here by design. SandboxPolicy values come from
the applied blueprint's infra IR in the registry (plan step 12) — only the
egress-proxy attribution env and the logical-image resolution live here.
"""


from pathlib import Path

from pydantic_settings import BaseSettings

from sandbox.egressproxy.credentials import sandbox_proxy_env


class TemporalSettings(BaseSettings):
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    task_queue: str = "poseidon-orchestrator"


class WorkerSettings(TemporalSettings):
    # Gateway credential (plan step 11): the projected service-account
    # token the kubelet mounts and rotates. Read per client construction —
    # clients live one activity, far shorter than the token's remaining
    # validity. The static token is the pre-step-11 fallback, deleted at 12.
    gateway_token_file: str = ""
    mcp_gateway_url: str
    agent_url: str
    target_repo: str
    base_ref: str = "main"
    # The bootstrap company this worker executes runs for, and its
    # namespace (where sandbox Jobs land). Single-company wiring like
    # AGENT_URL; goes per-run at plan step 16.
    company: str = "hello-saas"
    sandbox_namespace: str = "hello-saas"
    # Deploy-time resolution of the infra IR's *logical* toolchain image
    # (plan step 12): a blueprint says `poseidon-sandbox`; only the
    # deployment knows the content-addressed reference the cluster actually
    # carries. Any other image the IR names passes through untouched.
    sandbox_image: str = "poseidon-sandbox"
    # Egress attribution (§2b): the proxy URL as sandboxes reach it and the
    # shared HMAC key for per-run credentials. Both empty disables the env
    # injection entirely (proxy-less clusters keep zero egress).
    egress_proxy_url: str = ""
    egress_proxy_key: str = ""
    # The §2b platform signing key (plan step 16a): the orchestrator's
    # fulfill activities mint short-TTL ephemeral JWTs with it; both
    # gateways verify. Empty disables minting, never verification.
    platform_jwt_key: str = ""
    ephemeral_ttl_s: int = 900
    # Crew-pod provisioning (plan step 16c): the agent image the ephemeral
    # executor's brain host runs (content-addressed on kind, like the
    # sandbox image), and the platform namespace for gateway FQDNs.
    agent_image: str = ""
    platform_namespace: str = "poseidon-platform"
    # The standing PM's dispatch address (plan step 18: intake dispatches
    # the conductor exactly as implement dispatches an executor).
    pm_url: str = ""
    model: str = "mock-brain"
    max_iterations: int = 10
    poll_interval_s: float = 2

    def gateway_token(self) -> str:
        if self.gateway_token_file:
            return Path(self.gateway_token_file).read_text().strip()
        raise RuntimeError(
            "no gateway credential: set GATEWAY_TOKEN_FILE to the projected "
            "service-account token path"
        )

    def sandbox_proxy_env(self, run_id: str) -> dict[str, str]:
        if self.egress_proxy_url and self.egress_proxy_key:
            return sandbox_proxy_env(self.egress_proxy_url, self.egress_proxy_key, run_id)
        return {}
