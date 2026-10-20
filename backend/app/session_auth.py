"""Local verification of control-plane session tokens (no per-request HTTP)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import logging
import os
import threading
import time
from typing import Any

logger = logging.getLogger("uvicorn.error")

_SESSION_TTL_S = int(float(os.environ.get("SESSION_TTL_DAYS", "30") or 30) * 86400)
# Legacy cs1 tokens: max age from issued time, plus optional absolute cutoff.
_CS1_MAX_AGE_S = int(float(os.environ.get("CS1_MAX_AGE_DAYS", "7") or 7) * 86400)

_engine_lock = threading.Lock()
_cp_engine = None


class SessionLookupUnavailable(Exception):
    """cp_sessions could not be queried (fail closed in production → 503)."""


def _is_production() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def _b64url_decode(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def session_secret() -> str:
    """Return configured SESSION_SECRET, or empty. Never invents a default."""
    return (os.environ.get("SESSION_SECRET") or "").strip()


def session_secrets() -> list[str]:
    """Current secret, then SESSION_SECRET_PREVIOUS for one rotation overlap.

    New tokens are signed with the current secret only.
    """
    current = session_secret()
    previous = (os.environ.get("SESSION_SECRET_PREVIOUS") or "").strip()
    out: list[str] = []
    for secret in (current, previous):
        if secret and secret not in out:
            out.append(secret)
    return out


def _signature_ok(secrets: list[str], payload: bytes, sig: bytes) -> bool:
    for secret in secrets:
        expect = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
        if hmac.compare_digest(sig, expect):
            return True
    return False


def require_session_secret_configured() -> str:
    """Fail closed: missing/empty SESSION_SECRET is a hard error (no default)."""
    secret = session_secret()
    if not secret:
        raise RuntimeError(
            "SESSION_SECRET is required for APP_MODE=analysis and must not be empty. "
            "Set the same value on casesifter-analysis as on casesifter-control."
        )
    return secret


def _cs1_still_accepted(issued: int) -> bool:
    """Accept cs1 only within max age and before optional absolute cutoff."""
    now = time.time()
    raw_cutoff = (os.environ.get("CS1_ACCEPT_UNTIL") or "").strip()
    if raw_cutoff:
        try:
            if raw_cutoff.isdigit() or (raw_cutoff.startswith("-") is False and raw_cutoff.replace(".", "", 1).isdigit()):
                cutoff = float(raw_cutoff)
            else:
                from datetime import datetime, timezone

                cutoff = datetime.fromisoformat(raw_cutoff.replace("Z", "+00:00")).timestamp()
            if now >= cutoff:
                return False
        except Exception:
            return False
    if _CS1_MAX_AGE_S > 0 and (now - issued) > _CS1_MAX_AGE_S:
        return False
    return True


def parse_session_token(token: str) -> dict[str, Any] | None:
    secrets = session_secrets()
    # Fail closed: no secret → no local session acceptance (never use a default).
    if not token or not secrets:
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
        if not _signature_ok(secrets, payload, sig):
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
        if not _signature_ok(secrets, payload, sig):
            return None
        try:
            email, issued_s, _nonce = payload.decode("utf-8").split("|", 2)
            issued = int(issued_s)
        except Exception:
            return None
        if not _cs1_still_accepted(issued):
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


def _get_cp_engine():
    """Reuse one engine for cp_sessions lookups (no per-request create_engine)."""
    global _cp_engine
    with _engine_lock:
        if _cp_engine is not None:
            return _cp_engine
        try:
            from sqlalchemy import create_engine

            url = (os.environ.get("DATABASE_URL") or "").strip()
            if not url:
                sqlite = (os.environ.get("AUTH_SQLITE_PATH") or "").strip()
                if not sqlite:
                    return None
                url = f"sqlite:///{sqlite}"
            if url.startswith("postgres://"):
                url = "postgresql://" + url[len("postgres://") :]
            connect_args = {"check_same_thread": False} if url.startswith("sqlite") else {}
            _cp_engine = create_engine(
                url, future=True, pool_pre_ping=True, connect_args=connect_args
            )
            return _cp_engine
        except Exception as exc:
            logger.warning("cp_sessions engine init failed: %s", type(exc).__name__)
            if _is_production():
                raise SessionLookupUnavailable(str(exc)) from exc
            return None


def reset_cp_engine_for_tests() -> None:
    global _cp_engine
    with _engine_lock:
        if _cp_engine is not None:
            _cp_engine.dispose()
        _cp_engine = None


def session_row_active(sid: str) -> bool | None:
    """Return True/False when cp_sessions is readable; None if DB not configured.

    In production, lookup errors raise SessionLookupUnavailable (caller → 503).
    """
    if not sid:
        return None
    try:
        eng = _get_cp_engine()
        if eng is None:
            return None
        from sqlalchemy import text

        with eng.connect() as conn:
            row = conn.execute(
                text(
                    "SELECT revoked_at, expires_at FROM cp_sessions WHERE id = :id LIMIT 1"
                ),
                {"id": sid},
            ).first()
        if row is None:
            return False
        revoked_at, expires_at = row
        if revoked_at is not None:
            return False
        if expires_at is not None and float(expires_at) < time.time():
            return False
        return True
    except SessionLookupUnavailable:
        raise
    except Exception as exc:
        logger.warning("cp_sessions lookup failed: %s", type(exc).__name__)
        if _is_production():
            raise SessionLookupUnavailable(str(exc)) from exc
        return None


def verify_bearer_locally(token: str) -> dict[str, Any] | None:
    """Authenticate from HMAC (+ session revoke check). No HTTP.

    Raises SessionLookupUnavailable when revoke check cannot run in production.
    """
    parsed = parse_session_token(token)
    if not parsed:
        return None
    sid = parsed.get("sid")
    if sid:
        active = session_row_active(sid)
        if active is False:
            return None
        # active True, or None (DB not configured in non-prod) → accept HMAC.
    return {
        "auth": "session",
        "email": parsed.get("email"),
        "user_id": parsed.get("user_id"),
        "sid": sid,
        "authenticated": True,
    }
