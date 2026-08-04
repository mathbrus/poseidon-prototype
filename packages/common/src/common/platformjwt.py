"""Platform-signed short-TTL JWTs — the §2b ephemeral-identity contract.

The platform (orchestrator fulfill activities, plan step 16a) mints; both
gateways verify. Signature verification alone is never enough: the gateway
must also find a live `run_participants` row, so revocation is a registry
row termination and takes effect before the TTL runs out. That second half
lives with the verifier; this module owns only the token format.

Compact JWS, HS256, stdlib only. The key is a platform secret shared by
minter and verifiers (`platform-jwt-key`); it never reaches a company
namespace, so nothing an agent can read allows it to mint an identity.
"""

import base64
import hashlib
import hmac
import json
import time
from typing import Any

ISSUER = "poseidon-platform"
_HEADER = {"alg": "HS256", "typ": "JWT"}


class TokenError(Exception):
    """Verification failure; `reason` is a stable slug fit for an audit rule."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


def _sign(key: str, signing_input: bytes) -> bytes:
    return hmac.new(key.encode(), signing_input, hashlib.sha256).digest()


def mint(
    key: str,
    claims: dict[str, Any],
    audience: str,
    ttl_s: int,
    now: float | None = None,
) -> str:
    """Sign `claims` (the §5.1 identity fields) into a compact JWT."""
    issued = int(now if now is not None else time.time())
    payload = {
        **claims,
        "iss": ISSUER,
        "aud": audience,
        "iat": issued,
        "exp": issued + ttl_s,
    }
    signing_input = (
        _b64(json.dumps(_HEADER, separators=(",", ":")).encode())
        + "."
        + _b64(json.dumps(payload, separators=(",", ":"), sort_keys=True).encode())
    ).encode()
    return signing_input.decode() + "." + _b64(_sign(key, signing_input))


def issuer_of(token: str) -> str | None:
    """The *unverified* `iss` claim — for routing a bearer token to the right
    verifier (platform JWT vs. Kubernetes TokenReview), never for trust."""
    parts = token.split(".")
    if len(parts) != 3:
        return None
    try:
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, UnicodeDecodeError):
        return None
    return claims.get("iss") if isinstance(claims, dict) else None


def verify(
    key: str, token: str, audience: str, now: float | None = None
) -> dict[str, Any]:
    """Verify signature, audience and expiry; return the claims.

    Raises TokenError with a stable reason slug: bad_token, bad_signature,
    wrong_audience, expired.
    """
    parts = token.split(".")
    if len(parts) != 3:
        raise TokenError("bad_token")
    signing_input = (parts[0] + "." + parts[1]).encode()
    try:
        signature = _unb64(parts[2])
        header = json.loads(_unb64(parts[0]))
        claims = json.loads(_unb64(parts[1]))
    except (ValueError, UnicodeDecodeError) as exc:
        raise TokenError("bad_token") from exc
    if not isinstance(header, dict) or not isinstance(claims, dict):
        raise TokenError("bad_token")
    # The algorithm is pinned, never read from the header: an attacker
    # controls the header, and honoring it is the classic JWT downgrade.
    if header.get("alg") != "HS256":
        raise TokenError("bad_token")
    if not hmac.compare_digest(signature, _sign(key, signing_input)):
        raise TokenError("bad_signature")
    if claims.get("iss") != ISSUER:
        raise TokenError("bad_token")
    if claims.get("aud") != audience:
        raise TokenError("wrong_audience")
    current = now if now is not None else time.time()
    if not isinstance(claims.get("exp"), (int, float)) or claims["exp"] <= current:
        raise TokenError("expired")
    return claims
