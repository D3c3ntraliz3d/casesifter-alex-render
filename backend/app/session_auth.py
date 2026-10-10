"""Local verification of control-plane session tokens (no per-request HTTP)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import time
from typing import Any

logger = logging.getLogger("uvicorn.error")

_SESSION_TTL_S = int(float(os.environ.get("SESSION_TTL_DAYS", "30") or 30) * 86400)


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def session_secret() -> str:
    return (os.environ.get("SESSION_SECRET") or "").strip()


def parse_session_token(token: str) -> dict[str, Any] | None:
    secret = session_secret()
    if not token or not secret:
        return None
    if token.startswith("cs2."):
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = _b64url_decode(parts[1])
            sig = _b64url_decode(parts[2])
        except Exception:
            return None
        expect = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            user_id, email, sid, issued_s = payload.decode("utf-8").split("|", 3)
            issued = int(issued_s)
        except Exception:
            return None
        if _SESSION_TTL_S > 0 and (time.time() - issued) > _SESSION_TTL_S:
            return None
        return {
            "user_id": user_id,
            "email": email.lower().strip(),
            "sid": sid,
            "issued": issued,
        }
    if token.startswith("cs1."):
        parts = token.split(".")
        if len(parts) != 3:
            return None
        try:
            payload = _b64url_decode(parts[1])
            sig = _b64url_decode(parts[2])
        except Exception:
            return None
        expect = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if not hmac.compare_digest(sig, expect):
            return None
        try:
            email, issued_s, _nonce = payload.decode("utf-8").split("|", 2)
            issued = int(issued_s)
        except Exception:
            return None
        if _SESSION_TTL_S > 0 and (time.time() - issued) > _SESSION_TTL_S:
            return None
        return {
            "user_id": None,
            "email": email.lower().strip(),
            "sid": None,
            "issued": issued,
            "legacy": True,
        }
    return None


def session_row_active(sid: str) -> bool | None:
    """Return True/False if cp_sessions is readable; None if table unavailable."""
    if not sid:
        return None
    try:
        from sqlalchemy import create_engine, text

        url = (os.environ.get("DATABASE_URL") or "").strip()
        if not url:
            # Fall back to shared SQLite path used by control plane in tests.
            sqlite = (os.environ.get("AUTH_SQLITE_PATH") or "").strip()
            if not sqlite:
                return None
            url = f"sqlite:///{sqlite}"
        if url.startswith("postgres://"):
            url = "postgresql://" + url[len("postgres://") :]
        eng = create_engine(url, future=True, pool_pre_ping=True)
        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT revoked_at, expires_at FROM cp_sessions WHERE id = :id LIMIT 1"
                ),
                {"id": sid},
            ).first()
        eng.dispose()
        if row is None:
            return False
        revoked_at, expires_at = row
        if revoked_at is not None:
            return False
        if expires_at is not None and float(expires_at) < time.time():
            return False
        return True
    except Exception as exc:
        logger.debug("cp_sessions lookup unavailable: %s", exc)
        return None


def verify_bearer_locally(token: str) -> dict[str, Any] | None:
    """Authenticate from HMAC (+ optional session revoke check). No HTTP."""
    parsed = parse_session_token(token)
    if not parsed:
        return None
    sid = parsed.get("sid")
    if sid:
        active = session_row_active(sid)
        if active is False:
            return None
        # active is True or None (table missing — accept HMAC-only for cs2).
    return {
        "auth": "session",
        "email": parsed.get("email"),
        "user_id": parsed.get("user_id"),
        "sid": sid,
        "authenticated": True,
    }
