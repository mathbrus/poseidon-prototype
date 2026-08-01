"""Orchestrator settings.

Split in two: `TemporalSettings` is all a Temporal client needs (the CLI —
every field has a default suitable for a port-forwarded kind cluster);
`WorkerSettings` adds what activities need. The worker's only credential is
the orchestrator bearer token; GIT_TOKEN and provider keys never appear
here by design.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


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
    sandbox_image: str = "poseidon-sandbox"
    sandbox_network: str = "poseidon-kind-sandboxnet"
    runs_dir: Path = Path("runs")
    poll_interval_s: float = 2
