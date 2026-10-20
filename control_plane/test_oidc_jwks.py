"""OIDC JWKS signature verification (RSA)."""

from __future__ import annotations

import base64
import json
import time

import pytest
from cryptography.hazmat.primitives import hashes, serialization
from cryptography.hazmat.primitives.asymmetric import padding, rsa


def _b64url(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode("ascii").rstrip("=")


def _make_rsa_jwt(*, claims: dict, private_key, kid: str = "test-kid") -> str:
    header = {"alg": "RS256", "typ": "JWT", "kid": kid}
    h = _b64url(json.dumps(header, separators=(",", ":")).encode())
    p = _b64url(json.dumps(claims, separators=(",", ":")).encode())
    signing_input = f"{h}.{p}".encode("ascii")
    sig = private_key.sign(signing_input, padding.PKCS1v15(), hashes.SHA256())
    return f"{h}.{p}.{_b64url(sig)}"


@pytest.fixture()
def rsa_pair():
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    pub = key.public_key().public_numbers()

    def _int_b64(n: int) -> str:
        length = (n.bit_length() + 7) // 8
        return _b64url(n.to_bytes(length, "big"))

    jwk = {
        "kty": "RSA",
        "kid": "test-kid",
        "use": "sig",
        "alg": "RS256",
        "n": _int_b64(pub.n),
        "e": _int_b64(pub.e),
    }
    return key, jwk


def test_parse_id_token_verifies_jwks(monkeypatch, rsa_pair):
    import oidc_auth

    private_key, jwk = rsa_pair
    oidc_auth.reset_discovery_cache_for_tests()
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "casesifter-client")
    monkeypatch.setenv("OIDC_CLIENT_SECRET", "secret")
    monkeypatch.setenv("OIDC_REDIRECT_URI", "https://control.example/callback")

    monkeypatch.setattr(
        oidc_auth,
        "discovery",
        lambda: {
            "jwks_uri": "https://idp.example.com/jwks",
            "authorization_endpoint": "https://idp.example.com/auth",
            "token_endpoint": "https://idp.example.com/token",
        },
    )
    monkeypatch.setattr(oidc_auth, "fetch_jwks", lambda force=False: [jwk])

    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "casesifter-client",
        "nonce": "nonce-1",
        "exp": now + 600,
        "iat": now,
        "email": "lawyer@firm.com",
        "email_verified": True,
        "sub": "sub-1",
        "name": "Lawyer",
    }
    token = _make_rsa_jwt(claims=claims, private_key=private_key)
    parsed = oidc_auth.parse_id_token(token, expected_nonce="nonce-1")
    assert parsed["email"] == "lawyer@firm.com"
    assert parsed["sub"] == "sub-1"


def test_parse_id_token_rejects_bad_signature(monkeypatch, rsa_pair):
    import oidc_auth

    private_key, jwk = rsa_pair
    other = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    oidc_auth.reset_discovery_cache_for_tests()
    monkeypatch.setenv("OIDC_ISSUER", "https://idp.example.com")
    monkeypatch.setenv("OIDC_CLIENT_ID", "casesifter-client")
    monkeypatch.setattr(
        oidc_auth,
        "discovery",
        lambda: {"jwks_uri": "https://idp.example.com/jwks"},
    )
    monkeypatch.setattr(oidc_auth, "fetch_jwks", lambda force=False: [jwk])

    now = int(time.time())
    claims = {
        "iss": "https://idp.example.com",
        "aud": "casesifter-client",
        "nonce": "n",
        "exp": now + 600,
        "email": "a@b.com",
        "email_verified": True,
        "sub": "x",
    }
    # Sign with a different key than JWKS.
    token = _make_rsa_jwt(claims=claims, private_key=other)
    with pytest.raises(ValueError, match="signature"):
        oidc_auth.parse_id_token(token, expected_nonce="n")


def test_app_post_login_uses_cs_code(monkeypatch):
    import oidc_auth

    monkeypatch.setenv("CASESIFTER_PUBLIC_URL", "https://app.casesifter.com")
    url = oidc_auth.app_post_login_url(token_query="abc123")
    assert "cs_code=abc123" in url
    assert "cs_token=" not in url
