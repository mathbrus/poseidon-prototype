"""Operator settings — read once from the environment the chart sets."""

from pydantic_settings import BaseSettings


class OperatorSettings(BaseSettings):
    # The namespace the platform services live in: what the company
    # namespaces' NetworkPolicies select, and the gateway FQDNs standing
    # agents are pointed at.
    platform_namespace: str = "poseidon-platform"
    # The standing agent brain host: content-addressed on kind, like the
    # sandbox image.
    agent_image: str = "poseidon-agent:latest"
    # Cluster-level agent config that rides every standing pod template.
    model: str = "mock-brain"
    target_repo: str = ""
    max_iterations: int = 10
    bundle_port: int = 8081
