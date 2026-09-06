"""Gateway settings: workload-authn knobs (shared shape, `common.authn`)
plus the gateway-internal ones."""

from common.authn import AuthnSettings


class McpGatewaySettings(AuthnSettings):
    """Every field is set by the chart's mcp-gateway template."""

    opa_url: str
    # The git host credential: this pod alone holds it.
    git_token: str
