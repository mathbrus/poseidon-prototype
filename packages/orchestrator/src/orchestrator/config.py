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


class TemporalSettings(BaseSettings):
    temporal_address: str = "127.0.0.1:7233"
    temporal_namespace: str = "default"
    task_queue: str = "poseidon-orchestrator"


class WorkerSettings(TemporalSettings):
    orchestrator_bearer_token: str
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
    runs_dir: Path = Path("runs")
    poll_interval_s: float = 2

    def sandbox_policy(self) -> SandboxPolicy:
        return SandboxPolicy(
            image=self.sandbox_image,
            cpu=self.sandbox_cpu,
            memory=self.sandbox_memory,
            disk=self.sandbox_disk,
            deadline_s=self.sandbox_deadline_s,
        )
