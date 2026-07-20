"""Runner settings: the C7 subset the runner needs plus runner-local knobs.

The runner runs on the host (outside compose), so it also knows where the
agent is reachable, which sandbox image/network to use (the compose project
network name is passed via SANDBOX_NETWORK), and where run artifacts live.
"""

from pathlib import Path

from pydantic_settings import BaseSettings


class RunnerSettings(BaseSettings):
    runner_bearer_token: str
    mcp_gateway_url: str
    target_repo: str
    base_ref: str = "main"
    agent_url: str = "http://localhost:8000"
    sandbox_image: str = "poseidon-sandbox"
    sandbox_network: str = "sandboxnet"
    runs_dir: Path = Path("runs")
    step_timeout_s: float = 300
    agent_timeout_s: float = 3600
    poll_interval_s: float = 2

    @property
    def audit_path(self) -> Path:
        return self.runs_dir / "audit.jsonl"
