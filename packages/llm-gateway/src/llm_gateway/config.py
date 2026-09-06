"""LLM gateway settings.

Inherits the shared workload-authn shape (`common.authn`) — both gateways
verify the same credentials — and adds the metering knobs. Provider keys
never appear here: the upstream (LiteLLM with the keys, or the mock brain)
is reachable only from this service, so this is the single audited,
metered, authenticated route to any model.
"""

from common.authn import AuthnSettings


class LlmGatewaySettings(AuthnSettings):
    """Every field is set by the chart's llm-gateway template."""

    # Where completions are forwarded: the LiteLLM container holding the
    # provider keys (REAL_LLM) or the deterministic mock. Sealed by
    # NetworkPolicy so nothing but this gateway reaches it.
    upstream_url: str
    # The one model this deployment serves, pinned into every request: the
    # caller's own model field is never trusted for spend. The same value
    # reaches the agent pods, whose SDK shapes requests by the name.
    model: str
