"""Orchestrator settings.

Split in two: `TemporalSettings` is all a Temporal client needs (the CLI —
every field has a default suitable for a port-forwarded kind cluster);
`WorkerSettings` adds what activities need. The worker's only credential is
the orchestrator bearer token; GIT_TOKEN and provider keys never appear
here by design.
"""


from pathlib import Path

from pydantic_settings import BaseSettings

from sandbox.contract import SandboxPolicy
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
    orchestrator_bearer_token: str = ""
    mcp_gateway_url: str
    agent_url: str
    target_repo: str
    base_ref: str = "main"
    # Where sandbox Jobs run: the company namespace (hardcoded like the rest
    # of the company wiring until the operator exists, plan step 10).
    sandbox_namespace: str = "hello-saas"
    # Hand-authored SandboxPolicy values (sequencing choice 2) — replaced by
    # the compiler-emitted infra IR at plan step 12.
    sandbox_image: str = "poseidon-sandbox"
    sandbox_cpu: str = "1"
    sandbox_memory: str = "1Gi"
    sandbox_disk: str = "2Gi"
    sandbox_deadline_s: int = 3600
    # Egress attribution (§2b): the proxy URL as sandboxes reach it and the
    # shared HMAC key for per-run credentials. Both empty disables the env
    # injection entirely (pre-proxy clusters keep zero egress).
    egress_proxy_url: str = ""
    egress_proxy_key: str = ""
    poll_interval_s: float = 2

    def gateway_token(self) -> str:
        if self.gateway_token_file:
            return Path(self.gateway_token_file).read_text().strip()
        if self.orchestrator_bearer_token:
            return self.orchestrator_bearer_token
        raise RuntimeError(
            "no gateway credential: set GATEWAY_TOKEN_FILE (projected token) "
            "or ORCHESTRATOR_BEARER_TOKEN (static, pre-step-11)"
        )

    def sandbox_policy(self, run_id: str) -> SandboxPolicy:
        env: dict[str, str] = {}
        if self.egress_proxy_url and self.egress_proxy_key:
            env = sandbox_proxy_env(self.egress_proxy_url, self.egress_proxy_key, run_id)
        return SandboxPolicy(
            image=self.sandbox_image,
            cpu=self.sandbox_cpu,
            memory=self.sandbox_memory,
            disk=self.sandbox_disk,
            deadline_s=self.sandbox_deadline_s,
            env=env,
        )
