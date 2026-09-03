"""Operator settings — read once from the environment the chart sets."""

from pydantic_settings import BaseSettings

from common.topology import PLATFORM_NAMESPACE_DEFAULT


class OperatorSettings(BaseSettings):
    # The namespace the platform services live in: what the company
    # namespaces' NetworkPolicies select, and the gateway FQDNs standing
    # agents are pointed at. The chart sets it from the release namespace.
    platform_namespace: str = PLATFORM_NAMESPACE_DEFAULT
    # The agent image standing agents run — a content-addressed reference,
    # never a floating tag; the chart sets it from its image values.
    agent_image: str
    # Cluster-level agent config that rides every standing pod template.
    model: str = "mock-brain"
    target_repo: str = ""
    verify_cycles: int = 10
