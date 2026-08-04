"""Gateway settings: workload-authn knobs (shared shape, `common.authn`)
plus the gateway-internal ones."""

from common.authn import WorkloadAuthnSettings


class GatewaySettings(WorkloadAuthnSettings):
    opa_url: str = "http://opa:8181"
    # Git backend (C7: GIT_TOKEN/BASE_REF live in the gateway only).
    git_token: str | None = None
    base_ref: str = "main"
    github_api_url: str = "https://api.github.com"
