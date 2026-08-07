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


def mint(key: str, run_id: str) -> str:
    return hmac.new(key.encode(), run_id.encode(), hashlib.sha256).hexdigest()


def verify(key: str, run_id: str, secret: str) -> bool:
    return hmac.compare_digest(mint(key, run_id), secret)


def resolve_host(proxy_url: str) -> str:
    """`proxy_url` with its hostname replaced by an address.

    The sandbox has no DNS: cluster DNS is not in its NetworkPolicy egress,
    because a resolver that forwards unknown names upstream is a channel
    the audited proxy cannot see. Nothing else in a sandbox needs to
    resolve a name — with an HTTP proxy the *proxy* resolves the target
    host — so the one name the sandbox would have needed is resolved here,
    by the orchestrator, at provision time.

    An unresolvable host is returned unchanged: the run then fails at the
    proxy connection with a clear error rather than at provisioning.
    """
    scheme, sep, rest = proxy_url.partition("://")
    if not sep:
        return proxy_url
    hostport, slash, path = rest.partition("/")
    host, colon, port = hostport.rpartition(":")
    if not colon:  # no port: the whole thing is the host
        host, port = hostport, ""
    try:
        address = socket.gethostbyname(host)
    except OSError:
        return proxy_url
    return f"{scheme}://{address}{colon}{port}{slash}{path}"


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
