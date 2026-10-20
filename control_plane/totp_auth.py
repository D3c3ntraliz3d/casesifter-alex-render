"""TOTP enrollment and verification for password users.

Secrets are Fernet-encrypted with a key derived from TOTP_ENC_KEY or SESSION_SECRET.
Recovery codes are stored as argon2id hashes (one-time).
"""

from __future__ import annotations

import base64
import hashlib
import json
import os
import secrets
from typing import Any

try:
    import pyotp
except ImportError:  # pragma: no cover
    pyotp = None  # type: ignore[assignment]

try:
    from cryptography.fernet import Fernet, InvalidToken
except ImportError:  # pragma: no cover
    Fernet = None  # type: ignore[misc, assignment]
    InvalidToken = Exception  # type: ignore[misc, assignment]


ISSUER = (os.environ.get("TOTP_ISSUER") or "CaseSifter").strip() or "CaseSifter"
RECOVERY_CODE_COUNT = 8


def totp_available() -> bool:
    return pyotp is not None and Fernet is not None


def _fernet() -> Any:
    if Fernet is None:
        raise RuntimeError("cryptography is required for TOTP")
    raw = (os.environ.get("TOTP_ENC_KEY") or os.environ.get("SESSION_SECRET") or "").strip()
    if not raw:
        raise RuntimeError("TOTP_ENC_KEY or SESSION_SECRET is required to store authenticator secrets")
    digest = hashlib.sha256(raw.encode("utf-8")).digest()
    key = base64.urlsafe_b64encode(digest)
    return Fernet(key)


def encrypt_secret(secret: str) -> str:
    return _fernet().encrypt(secret.encode("utf-8")).decode("ascii")


def decrypt_secret(blob: str) -> str:
    try:
        return _fernet().decrypt(blob.encode("ascii")).decode("utf-8")
    except InvalidToken as exc:
        raise ValueError("Could not decrypt authenticator secret") from exc


def generate_secret() -> str:
    if pyotp is None:
        raise RuntimeError("pyotp is required")
    return pyotp.random_base32()


def provisioning_uri(secret: str, email: str) -> str:
    if pyotp is None:
        raise RuntimeError("pyotp is required")
    return pyotp.TOTP(secret).provisioning_uri(name=email, issuer_name=ISSUER)


def verify_code(secret: str, code: str, *, window: int = 1) -> bool:
    if pyotp is None:
        return False
    cleaned = "".join(ch for ch in str(code or "") if ch.isdigit())
    if len(cleaned) != 6:
        return False
    return bool(pyotp.TOTP(secret).verify(cleaned, valid_window=window))


def mint_recovery_codes(count: int = RECOVERY_CODE_COUNT) -> list[str]:
    return [secrets.token_hex(4).upper() for _ in range(max(1, min(count, 16)))]


def hash_recovery_codes(codes: list[str]) -> list[str]:
    import auth_store as auth

    return [auth.hash_password(code) for code in codes]


def serialize_recovery_hashes(hashes: list[str]) -> str:
    return json.dumps(hashes)


def load_recovery_hashes(blob: str | None) -> list[str]:
    if not blob:
        return []
    try:
        val = json.loads(blob)
        return [str(x) for x in val] if isinstance(val, list) else []
    except json.JSONDecodeError:
        return []


def consume_recovery_code(hashes: list[str], presented: str) -> list[str] | None:
    """Return remaining hashes if a code matched, else None."""
    import auth_store as auth

    cleaned = (presented or "").strip().upper().replace(" ", "")
    if not cleaned:
        return None
    for index, digest in enumerate(hashes):
        # verify_password expects a CpUser-like object; use PasswordHasher directly.
        if auth._ph is None:
            return None
        try:
            if auth._ph.verify(digest, cleaned):
                return hashes[:index] + hashes[index + 1 :]
        except Exception:
            continue
        # Also accept lowercase presentation against the same hash.
        try:
            if auth._ph.verify(digest, cleaned.lower()):
                return hashes[:index] + hashes[index + 1 :]
        except Exception:
            continue
    return None
