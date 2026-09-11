"""Orchestrator settings: what the worker and its activities need. The
worker's gateway credential is its projected service-account token;
GIT_TOKEN and provider keys never appear here by design. SandboxPolicy
values come from the applied blueprint's infra IR in the registry — only
the egress-proxy attribution env and the logical-image resolution live
here.
"""

from pathlib import Path
from typing import Literal

from pydantic_settings import BaseSettings

from sandbox.egressproxy.credentials import sandbox_proxy_env


class WorkerSettings(BaseSettings):
    """Every field is set by the chart's orchestrator-worker template; none
    has a default the deployment could silently rely on."""

    temporal_address: str
    # Gateway credential: the projected service-account token the kubelet
    # mounts and rotates. Read per client construction — clients live one
    # activity, far shorter than the token's remaining validity.
    gateway_token_file: str
    mcp_gateway_url: str
    # The workflow-recorded crew-request path evaluates the same compiled
    # decision the crew.request tool faces, directly at OPA.
    opa_url: str
    # The platform namespace, for the gateway addresses ephemeral pods get.
    platform_namespace: str
    # The content-addressed image the platform's own sandbox toolchain
    # resolves to (`common.topology.SANDBOX_TOOLCHAIN`); any other image a
    # blueprint names passes through untouched.
    sandbox_image: str
    # The agent image every ephemeral agent runs, and the model its pod is
    # told — the same value the LLM gateway pins.
    agent_image: str
    model: str
    # Egress attribution: the proxy URL as sandboxes reach it and the
    # shared HMAC key their per-run credentials are minted from.
    egress_proxy_url: str
    egress_proxy_key: str
    # Which activity-bound profile every workflow on this worker runs with
    # (`orchestrator.workflows.base.PROFILES`): `fast` for the mock brain,
    # `default` for a paid one.
    timeout_profile: Literal["default", "fast"]

    def gateway_token(self) -> str:
        return Path(self.gateway_token_file).read_text().strip()

    def sandbox_proxy_env(self, run_id: str) -> dict[str, str]:
        return sandbox_proxy_env(self.egress_proxy_url, self.egress_proxy_key, run_id)
