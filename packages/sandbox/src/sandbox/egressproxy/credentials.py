"""Per-run egress-proxy credentials — the attribution contract.

The credential is self-authenticating: username is the `run_id`, password is
HMAC(key, run_id). The orchestrator (which holds the shared key) mints it
into the sandbox environment at provision time; the proxy verifies it
statelessly on every request and maps it back to the run — no registration
round-trip, nothing to lose on a proxy restart, and unforgeable from inside
a sandbox that only ever sees its own credential.
"""

import hashlib
import hmac
import socket
from urllib.parse import urlsplit, urlunsplit


def mint(key: str, run_id: str) -> str:
    return hmac.new(key.encode(), run_id.encode(), hashlib.sha256).hexdigest()


def verify(key: str, run_id: str, secret: str) -> bool:
    return hmac.compare_digest(mint(key, run_id), secret)


class ProxyUnresolvable(Exception):
    """The egress proxy's hostname did not resolve at provision time."""


def resolve_host(proxy_url: str) -> str:
    """`proxy_url` with its hostname replaced by an address.

    The sandbox has no DNS: cluster DNS is not in its NetworkPolicy egress,
    because a resolver that forwards unknown names upstream is a channel
    the audited proxy cannot see. Nothing else in a sandbox needs to
    resolve a name — with an HTTP proxy the *proxy* resolves the target
    host — so the one name the sandbox would have needed is resolved here,
    by the orchestrator, at provision time.

    Failing to resolve raises rather than passing the hostname through: a
    sandbox with no resolver cannot use one, so a hostname here guarantees
    a DNS failure inside pip or curl, as far from the misconfiguration as
    it is possible to get. Raising fails the provisioning activity instead,
    where there is a resolver, a log and a retry.
    """
    parts = urlsplit(proxy_url)
    if not parts.hostname:
        raise ProxyUnresolvable(f"egress proxy URL has no host: {proxy_url!r}")
    try:
        address = socket.gethostbyname(parts.hostname)
    except OSError as exc:
        raise ProxyUnresolvable(
            f"egress proxy host {parts.hostname!r} did not resolve: {exc}"
        ) from exc
    netloc = address if parts.port is None else f"{address}:{parts.port}"
    return urlunsplit(parts._replace(netloc=netloc))


def sandbox_proxy_env(proxy_url: str, key: str, run_id: str) -> dict[str, str]:
    """The env block injected through `SandboxPolicy.env`:
    standard proxy variables carrying the per-run credential, which
    pip/npm/curl and friends honor without any sandbox-side setup.

    The proxy address is resolved here (see `resolve_host`) because the
    sandbox itself has no DNS.
    """
    scheme, _, rest = resolve_host(proxy_url).partition("://")
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
