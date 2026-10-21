"""Reconciler settings — read once from the environment the chart sets."""

from pydantic_settings import BaseSettings


class ReconcilerSettings(BaseSettings):
    """Every field is set by the chart's reconciler template."""

    # The namespace the platform services live in: what the company
    # namespaces' NetworkPolicies select, and the gateway addresses standing
    # agents are pointed at.
    platform_namespace: str
    # The agent image standing agents run — a content-addressed reference,
    # never a floating tag.
    agent_image: str
    # The model every standing pod is told — the same value the LLM gateway
    # pins.
    model: str
    # Where the reconciler reads which bundle revisions OPA has activated. A
    # read and nothing else: bundles reach OPA by OPA's own polling.
    opa_url: str
