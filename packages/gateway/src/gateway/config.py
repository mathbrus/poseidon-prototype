"""Gateway settings: the C7 tokens it needs plus gateway-internal knobs."""

from pydantic_settings import BaseSettings


class GatewaySettings(BaseSettings):
    opa_url: str = "http://opa:8181"
    # Git backend (C7: GIT_TOKEN/BASE_REF live in the gateway only).
    git_token: str | None = None
    base_ref: str = "main"
    github_api_url: str = "https://api.github.com"

    # Workload identity (plan step 11). Projected service-account tokens are
    # verified through the TokenReview API; platform-signed ephemeral JWTs
    # (§2b) against the shared platform key. The audience scopes both: a
    # token minted for anything else is rejected here.
    platform_namespace: str = "poseidon-platform"
    orchestrator_service_account: str = "orchestrator-worker"
    token_audience: str = "poseidon-gateway"
    # Empty disables the ephemeral-JWT path (nothing mints these until plan
    # step 16a; the verify seam is live from step 11 so 16a is mint-only).
    platform_jwt_key: str = ""
    # Empty means "derive from the in-cluster environment"; unit tests leave
    # all three alone and inject a reviewer instead.
    kube_api_url: str = ""
    kube_token_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/token"
    kube_ca_file: str = "/var/run/secrets/kubernetes.io/serviceaccount/ca.crt"
    # Successful TokenReviews are cached this long, so the API server sees
    # one review per caller per minute, not one per tool call. Revoking a
    # ServiceAccount takes up to this long to bite; ephemeral revocation
    # does not ride this cache (the participants row is checked every call).
    authn_cache_ttl_s: float = 60.0
