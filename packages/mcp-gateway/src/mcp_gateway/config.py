"""Gateway settings: workload-authn knobs (shared shape, `common.authn`)
plus the gateway-internal ones."""

from common.authn import WorkloadAuthnSettings


class McpGatewaySettings(WorkloadAuthnSettings):
    opa_url: str = "http://opa:8181"
    # Git backend: GIT_TOKEN lives in the gateway only.
    git_token: str | None = None
