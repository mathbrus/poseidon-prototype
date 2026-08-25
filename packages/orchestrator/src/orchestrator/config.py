"""Orchestrator settings.

Split in two: `TemporalSettings` is all a Temporal client needs (the CLI —
every field has a default suitable for a port-forwarded kind cluster);
`WorkerSettings` adds what activities need. The worker's gateway credential
is its projected service-account token; GIT_TOKEN and
provider keys never appear here by design. SandboxPolicy values come from
the applied blueprint's infra IR in the registry — only the
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
    # Gateway credential: the projected service-account
    # token the kubelet mounts and rotates. Read per client construction —
    # clients live one activity, far shorter than the token's remaining
    # validity.
    gateway_token_file: str = ""
    mcp_gateway_url: str
    target_repo: str
    base_ref: str = "main"
    # The bootstrap company this worker executes runs for, and its
    # namespace (where sandbox Jobs land). One worker serves one company.
    company: str = "hello-saas"
    sandbox_namespace: str = "hello-saas"
    # Deploy-time resolution of the infra IR's *logical* toolchain image:
    # a blueprint says `poseidon-sandbox`; only the
    # deployment knows the content-addressed reference the cluster actually
    # carries. Any other image the IR names passes through untouched.
    sandbox_image: str = "poseidon-sandbox"
    # Egress attribution: the proxy URL as sandboxes reach it and the
    # shared HMAC key for per-run credentials. Both empty disables the env
    # injection entirely (proxy-less clusters keep zero egress).
    egress_proxy_url: str = ""
    egress_proxy_key: str = ""
    # Crew-pod provisioning: the agent image the ephemeral
    # executor's brain host runs (content-addressed on kind, like the
    # sandbox image), and the platform namespace for gateway FQDNs.
    agent_image: str
    platform_namespace: str = "poseidon-platform"
    # The standing PM's dispatch address (intake dispatches
    # the conductor; implement dispatches ephemeral executors at their
    # crew-pod URLs — there is no standing executor address).
    pm_url: str = ""
    # The workflow-recorded crew-request path evaluates the
    # same compiled decision the crew.request tool faces, directly at OPA.
    opa_url: str = "http://opa:8181"
    model: str = "mock-brain"
    max_iterations: int = 10
    poll_interval_s: float = 2
    # How often the worker's ensure loop checks that the company's declared
    # heartbeat workflow is running (reconcile-on-goal: the applied
    # blueprint's heartbeat section is the goal; the workflow itself stops
    # when the section is gone).
    heartbeat_ensure_interval_s: float = 60

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
