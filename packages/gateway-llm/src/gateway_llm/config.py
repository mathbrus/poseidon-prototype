"""LLM gateway settings.

Inherits the shared workload-authn shape (`common.authn`) — both gateways
verify the same credentials — and adds the metering knobs. Provider keys
never appear here: the upstream (LiteLLM with the keys, or the mock brain)
is reachable only from this service, so this is the single audited,
metered, authenticated route to any model.
"""

import json

from common.authn import WorkloadAuthnSettings


class LlmGatewaySettings(WorkloadAuthnSettings):
    # Where completions are forwarded: the LiteLLM container holding the
    # provider keys (REAL_LLM) or the deterministic mock. Sealed by
    # NetworkPolicy so nothing but this gateway reaches it.
    upstream_url: str = "http://llm-proxy:4000"
    # tier -> model, from cluster config (the blueprint names tiers; only
    # the deployment knows which concrete model a tier means here). JSON
    # object, e.g. '{"frontier": "gpt-5.6-luna", "standard": "gpt-5.6-luna"}'.
    tier_models: str = ""
    # For unmapped tiers and callers without an instance row.
    default_model: str = "mock-brain"
    # Flat pricing per 1K tokens (prompt + completion). Per-model price
    # tables are a future concern; what matters is that spend accrues
    # from real usage and the cap bites.
    price_per_1k_tokens: float = 0.002

    def tier_model_map(self) -> dict[str, str]:
        if not self.tier_models:
            return {}
        parsed = json.loads(self.tier_models)
        if not isinstance(parsed, dict):
            raise ValueError("TIER_MODELS must be a JSON object of tier -> model")
        return {str(k): str(v) for k, v in parsed.items()}
