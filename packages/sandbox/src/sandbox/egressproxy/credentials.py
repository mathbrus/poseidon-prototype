"""Per-run egress-proxy credentials — the frozen §2b attribution contract.

The credential is self-authenticating: username is the `run_id`, password is
HMAC(key, run_id). The orchestrator (which holds the shared key) mints it
into the sandbox environment at provision time; the proxy verifies it
statelessly on every request and maps it back to the run — no registration
round-trip, nothing to lose on a proxy restart, and unforgeable from inside
a sandbox that only ever sees its own credential.
"""

import hashlib
import hmac


def mint(key: str, run_id: str) -> str:
    return hmac.new(key.encode(), run_id.encode(), hashlib.sha256).hexdigest()


def verify(key: str, run_id: str, secret: str) -> bool:
    return hmac.compare_digest(mint(key, run_id), secret)


def sandbox_proxy_env(proxy_url: str, key: str, run_id: str) -> dict[str, str]:
    """The env block injected through `SandboxPolicy.env` (plan step 4's
    seam): standard proxy variables carrying the per-run credential, which
    pip/npm/curl and friends honor without any sandbox-side setup."""
    scheme, _, rest = proxy_url.partition("://")
    authed = f"{scheme}://{run_id}:{mint(key, run_id)}@{rest}"
    no_proxy = "localhost,127.0.0.1"
    return {
        "HTTP_PROXY": authed,
        "HTTPS_PROXY": authed,
        "http_proxy": authed,
        "https_proxy": authed,
        "NO_PROXY": no_proxy,
        "no_proxy": no_proxy,
    }
