"""OIDC (OpenID Connect) authorization-code flow for optional firm SSO.

Configured via env (see docs/admin-sso-2fa.md). Supports Google, Microsoft Entra,
Okta, and other OIDC providers that expose a discovery document.

ID tokens are verified against the provider JWKS (RS256/ES256). SAML is a
documented enterprise follow-up — not implemented in this module.
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
import time
import urllib.error
import urllib.parse
import urllib.request
from typing import Any

# Optional cryptography for JWKS signature verify (required in production SSO).
try:
    from cryptography.hazmat.primitives.asymmetric import ec, padding, rsa
    from cryptography.hazmat.primitives import hashes
    from cryptography.hazmat.primitives.asymmetric.utils import encode_dss_signature
    from cryptography.hazmat.backends import default_backend
except ImportError:  # pragma: no cover
    ec = None  # type: ignore
    padding = None  # type: ignore
    rsa = None  # type: ignore
    hashes = None  # type: ignore
    encode_dss_signature = None  # type: ignore
    default_backend = None  # type: ignore


def oidc_configured() -> bool:
    return bool(
        (os.environ.get("OIDC_ISSUER") or "").strip()
        and (os.environ.get("OIDC_CLIENT_ID") or "").strip()
        and (os.environ.get("OIDC_CLIENT_SECRET") or "").strip()
        and (os.environ.get("OIDC_REDIRECT_URI") or "").strip()
    )


def trusts_idp_mfa() -> bool:
    """When true, local TOTP is skipped for users who signed in via SSO."""
    raw = (os.environ.get("OIDC_TRUSTS_IDP_MFA") or "true").strip().lower()
    return raw in {"1", "true", "yes", ""}


def allowed_email_domains() -> set[str] | None:
    """Optional allow-list. None means any verified email from the IdP is accepted."""
    raw = (os.environ.get("OIDC_ALLOWED_DOMAINS") or "").strip()
    if not raw:
        return None
    return {part.strip().lower() for part in raw.split(",") if part.strip()}


def _http_json(method: str, url: str, *, data: dict | None = None, headers: dict | None = None) -> dict[str, Any]:
    body = None
    req_headers = {"Accept": "application/json", "User-Agent": "CaseSifter-OIDC/1.0"}
    if headers:
        req_headers.update(headers)
    if data is not None:
        body = urllib.parse.urlencode(data).encode("utf-8")
        req_headers["Content-Type"] = "application/x-www-form-urlencoded"
    req = urllib.request.Request(url, data=body, headers=req_headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:400]
        raise RuntimeError(f"OIDC HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"OIDC unreachable: {exc}") from exc
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError("OIDC returned non-JSON") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("OIDC returned unexpected JSON")
    return parsed


_discovery_cache: dict[str, Any] = {"at": 0.0, "doc": None}
_jwks_cache: dict[str, Any] = {"at": 0.0, "keys": None, "uri": None}


def discovery() -> dict[str, Any]:
    issuer = (os.environ.get("OIDC_ISSUER") or "").strip().rstrip("/")
    if not issuer:
        raise RuntimeError("OIDC_ISSUER is not set")
    now = time.time()
    if _discovery_cache["doc"] and (now - float(_discovery_cache["at"] or 0)) < 3600:
        return _discovery_cache["doc"]
    url = f"{issuer}/.well-known/openid-configuration"
    doc = _http_json("GET", url)
    _discovery_cache["doc"] = doc
    _discovery_cache["at"] = now
    return doc


def reset_discovery_cache_for_tests() -> None:
    _discovery_cache["doc"] = None
    _discovery_cache["at"] = 0.0
    _jwks_cache["keys"] = None
    _jwks_cache["at"] = 0.0
    _jwks_cache["uri"] = None


def fetch_jwks(*, force: bool = False) -> list[dict[str, Any]]:
    """Load JWKS from discovery jwks_uri (cached ~1h)."""
    now = time.time()
    if (
        not force
        and _jwks_cache["keys"] is not None
        and (now - float(_jwks_cache["at"] or 0)) < 3600
    ):
        return list(_jwks_cache["keys"] or [])
    doc = discovery()
    uri = doc.get("jwks_uri")
    if not uri:
        raise RuntimeError("OIDC discovery missing jwks_uri")
    payload = _http_json("GET", uri)
    keys = payload.get("keys") if isinstance(payload, dict) else None
    if not isinstance(keys, list):
        raise RuntimeError("JWKS response missing keys")
    _jwks_cache["keys"] = keys
    _jwks_cache["at"] = now
    _jwks_cache["uri"] = uri
    return list(keys)


def _b64url_bytes(segment: str) -> bytes:
    pad = "=" * (-len(segment) % 4)
    return base64.urlsafe_b64decode(segment + pad)


def _b64url_json(segment: str) -> dict[str, Any]:
    data = json.loads(_b64url_bytes(segment).decode("utf-8"))
    if not isinstance(data, dict):
        raise ValueError("JWT segment is not an object")
    return data


def _int_from_b64(segment: str) -> int:
    return int.from_bytes(_b64url_bytes(segment), "big")


def _public_key_from_jwk(jwk: dict[str, Any]):
    if rsa is None or ec is None:
        raise RuntimeError("cryptography package is required for OIDC JWKS verification")
    kty = str(jwk.get("kty") or "")
    if kty == "RSA":
        n = _int_from_b64(str(jwk["n"]))
        e = _int_from_b64(str(jwk["e"]))
        return rsa.RSAPublicNumbers(e, n).public_key(default_backend())
    if kty == "EC":
        crv = str(jwk.get("crv") or "P-256")
        curves = {
            "P-256": ec.SECP256R1(),
            "P-384": ec.SECP384R1(),
            "P-521": ec.SECP521R1(),
        }
        curve = curves.get(crv)
        if curve is None:
            raise ValueError(f"Unsupported EC curve: {crv}")
        x = _int_from_b64(str(jwk["x"]))
        y = _int_from_b64(str(jwk["y"]))
        return ec.EllipticCurvePublicNumbers(x, y, curve).public_key(default_backend())
    raise ValueError(f"Unsupported JWK kty: {kty}")


def _find_jwk(header: dict[str, Any], keys: list[dict[str, Any]]) -> dict[str, Any]:
    kid = header.get("kid")
    if kid:
        for key in keys:
            if key.get("kid") == kid:
                return key
        # Force refresh once on unknown kid (rotation).
        keys = fetch_jwks(force=True)
        for key in keys:
            if key.get("kid") == kid:
                return key
        raise ValueError("No JWKS key matches kid")
    if len(keys) == 1:
        return keys[0]
    raise ValueError("ID token header missing kid and JWKS has multiple keys")


def verify_id_token_signature(id_token: str) -> dict[str, Any]:
    """Verify JWS signature via JWKS; return header + claims. Rejects alg=none."""
    parts = (id_token or "").split(".")
    if len(parts) != 3:
        raise ValueError("Malformed ID token")
    header = _b64url_json(parts[0])
    alg = str(header.get("alg") or "")
    if not alg or alg.lower() == "none":
        raise ValueError("ID token algorithm not allowed")
    if alg not in {"RS256", "RS384", "RS512", "ES256", "ES384", "ES512"}:
        raise ValueError(f"Unsupported ID token alg: {alg}")
    keys = fetch_jwks()
    jwk = _find_jwk(header, keys)
    public_key = _public_key_from_jwk(jwk)
    signing_input = f"{parts[0]}.{parts[1]}".encode("ascii")
    signature = _b64url_bytes(parts[2])
    hash_map = {
        "RS256": hashes.SHA256(),
        "RS384": hashes.SHA384(),
        "RS512": hashes.SHA512(),
        "ES256": hashes.SHA256(),
        "ES384": hashes.SHA384(),
        "ES512": hashes.SHA512(),
    }
    digest = hash_map[alg]
    try:
        if alg.startswith("RS"):
            public_key.verify(signature, signing_input, padding.PKCS1v15(), digest)
        else:
            # ECDSA JWT signatures are raw r||s; cryptography expects DER.
            size = {256: 32, 384: 48, 512: 66}[int(alg[2:])]
            if len(signature) != size * 2:
                raise ValueError("Invalid ECDSA signature length")
            r = int.from_bytes(signature[:size], "big")
            s_int = int.from_bytes(signature[size:], "big")
            der = encode_dss_signature(r, s_int)
            public_key.verify(der, signing_input, ec.ECDSA(digest))
    except Exception as exc:
        raise ValueError("ID token signature verification failed") from exc
    claims = _b64url_json(parts[1])
    return {"header": header, "claims": claims}


def _pkce_pair() -> tuple[str, str]:
    verifier = secrets.token_urlsafe(48)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode("ascii")).digest()).decode("ascii").rstrip("=")
    return verifier, challenge


def build_authorize_redirect(
    *,
    state: str,
    nonce: str,
    code_verifier: str | None = None,
) -> tuple[str, str]:
    """Return (authorize_url, code_verifier)."""
    doc = discovery()
    authorize = doc.get("authorization_endpoint")
    if not authorize:
        raise RuntimeError("OIDC discovery missing authorization_endpoint")
    if code_verifier:
        verifier = code_verifier
        challenge = base64.urlsafe_b64encode(
            hashlib.sha256(verifier.encode("ascii")).digest()
        ).decode("ascii").rstrip("=")
    else:
        verifier, challenge = _pkce_pair()
    params = {
        "client_id": (os.environ.get("OIDC_CLIENT_ID") or "").strip(),
        "response_type": "code",
        "scope": (os.environ.get("OIDC_SCOPES") or "openid email profile").strip(),
        "redirect_uri": (os.environ.get("OIDC_REDIRECT_URI") or "").strip(),
        "state": state,
        "nonce": nonce,
        "code_challenge": challenge,
        "code_challenge_method": "S256",
    }
    prompt = (os.environ.get("OIDC_PROMPT") or "").strip()
    if prompt:
        params["prompt"] = prompt
    return f"{authorize}?{urllib.parse.urlencode(params)}", verifier


def exchange_code(*, code: str, code_verifier: str) -> dict[str, Any]:
    doc = discovery()
    token_url = doc.get("token_endpoint")
    if not token_url:
        raise RuntimeError("OIDC discovery missing token_endpoint")
    return _http_json(
        "POST",
        token_url,
        data={
            "grant_type": "authorization_code",
            "code": code,
            "redirect_uri": (os.environ.get("OIDC_REDIRECT_URI") or "").strip(),
            "client_id": (os.environ.get("OIDC_CLIENT_ID") or "").strip(),
            "client_secret": (os.environ.get("OIDC_CLIENT_SECRET") or "").strip(),
            "code_verifier": code_verifier,
        },
    )


def parse_id_token(id_token: str, *, expected_nonce: str) -> dict[str, Any]:
    """Verify JWKS signature and validate standard OIDC claims."""
    verified = verify_id_token_signature(id_token)
    claims = verified["claims"]
    issuer = (os.environ.get("OIDC_ISSUER") or "").strip().rstrip("/")
    if issuer and str(claims.get("iss") or "").rstrip("/") != issuer:
        raise ValueError("ID token issuer mismatch")
    aud = claims.get("aud")
    client_id = (os.environ.get("OIDC_CLIENT_ID") or "").strip()
    if isinstance(aud, list):
        if client_id not in aud:
            raise ValueError("ID token audience mismatch")
    elif str(aud or "") != client_id:
        raise ValueError("ID token audience mismatch")
    if expected_nonce and str(claims.get("nonce") or "") != expected_nonce:
        raise ValueError("ID token nonce mismatch")
    now = time.time()
    exp = claims.get("exp")
    if exp is not None and float(exp) < now - 30:
        raise ValueError("ID token expired")
    nbf = claims.get("nbf")
    if nbf is not None and float(nbf) > now + 30:
        raise ValueError("ID token not yet valid")
    iat = claims.get("iat")
    if iat is not None and float(iat) > now + 120:
        raise ValueError("ID token iat is in the future")
    email = str(claims.get("email") or "").strip().lower()
    if not email:
        raise ValueError("ID token missing email")
    if claims.get("email_verified") is False:
        raise ValueError("IdP email is not verified")
    domains = allowed_email_domains()
    if domains is not None:
        domain = email.rsplit("@", 1)[-1]
        if domain not in domains:
            raise ValueError("Email domain is not allowed for SSO")
    return {
        "email": email,
        "sub": str(claims.get("sub") or ""),
        "name": str(claims.get("name") or claims.get("preferred_username") or ""),
        "iss": str(claims.get("iss") or issuer),
        "claims": claims,
    }


def app_post_login_url(*, token_query: str | None = None) -> str:
    """Redirect to the web app with a one-time exchange code (?cs_code=)."""
    base = (os.environ.get("CASESIFTER_PUBLIC_URL") or "http://127.0.0.1:5173").rstrip("/")
    if token_query:
        return f"{base}/?cs_code={urllib.parse.quote(token_query)}"
    return f"{base}/"
