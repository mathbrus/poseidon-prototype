"""The platform JWT format: mint/verify round-trip and every
rejection reason a gateway will turn into an audit rule."""

import pytest

from common import platformjwt

KEY = "test-platform-key"
AUD = "poseidon-gateway"
NOW = 1_754_000_000.0

CLAIMS = {
    "sub": "feature-delivery-executor-01X",
    "company": "hello-saas",
    "role": "backend-engineer",
    "lifecycle": "ephemeral",
    "run_id": "01RUN",
    "parent_agent_id": "pm-1",
}


def test_round_trip() -> None:
    token = platformjwt.mint(KEY, CLAIMS, AUD, ttl_s=600, now=NOW)
    claims = platformjwt.verify(KEY, token, AUD, now=NOW + 1)
    for key, value in CLAIMS.items():
        assert claims[key] == value
    assert claims["iss"] == platformjwt.ISSUER
    assert claims["exp"] == int(NOW) + 600


def test_issuer_of_routes_platform_tokens() -> None:
    token = platformjwt.mint(KEY, CLAIMS, AUD, ttl_s=600, now=NOW)
    assert platformjwt.issuer_of(token) == platformjwt.ISSUER
    assert platformjwt.issuer_of("not-a-jwt") is None
    assert platformjwt.issuer_of("a.b") is None
    assert platformjwt.issuer_of("a.!!!.c") is None


@pytest.mark.parametrize(
    ("mutate", "reason"),
    [
        (lambda t: t + "x", "bad_signature"),
        (lambda t: "garbage", "bad_token"),
        (lambda t: t.rsplit(".", 1)[0], "bad_token"),
    ],
)
def test_tampering(mutate, reason) -> None:
    token = platformjwt.mint(KEY, CLAIMS, AUD, ttl_s=600, now=NOW)
    with pytest.raises(platformjwt.TokenError) as err:
        platformjwt.verify(KEY, mutate(token), AUD, now=NOW + 1)
    assert err.value.reason == reason


def test_wrong_key() -> None:
    token = platformjwt.mint("other-key", CLAIMS, AUD, ttl_s=600, now=NOW)
    with pytest.raises(platformjwt.TokenError) as err:
        platformjwt.verify(KEY, token, AUD, now=NOW + 1)
    assert err.value.reason == "bad_signature"


def test_wrong_audience() -> None:
    token = platformjwt.mint(KEY, CLAIMS, "poseidon-llm-gateway", ttl_s=600, now=NOW)
    with pytest.raises(platformjwt.TokenError) as err:
        platformjwt.verify(KEY, token, AUD, now=NOW + 1)
    assert err.value.reason == "wrong_audience"


def test_expired() -> None:
    token = platformjwt.mint(KEY, CLAIMS, AUD, ttl_s=600, now=NOW)
    with pytest.raises(platformjwt.TokenError) as err:
        platformjwt.verify(KEY, token, AUD, now=NOW + 601)
    assert err.value.reason == "expired"


def test_alg_is_pinned() -> None:
    """A token whose header claims another alg is rejected outright — the
    header is attacker-controlled and must never pick the verifier."""
    import base64
    import json

    token = platformjwt.mint(KEY, CLAIMS, AUD, ttl_s=600, now=NOW)
    _, payload, signature = token.split(".")
    forged_header = (
        base64.urlsafe_b64encode(json.dumps({"alg": "none", "typ": "JWT"}).encode())
        .rstrip(b"=")
        .decode()
    )
    with pytest.raises(platformjwt.TokenError) as err:
        platformjwt.verify(KEY, f"{forged_header}.{payload}.{signature}", AUD, now=NOW)
    assert err.value.reason == "bad_token"
