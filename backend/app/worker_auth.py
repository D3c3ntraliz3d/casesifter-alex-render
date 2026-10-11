"""Shared-secret auth for the internal worker → analysis result API."""

from __future__ import annotations

import hashlib
import hmac
import time

from fastapi import Header, HTTPException

from app.config import settings


def worker_service_token() -> str:
    return (settings.worker_service_token or "").strip()


def require_worker_token_configured() -> None:
    if not worker_service_token():
        raise RuntimeError("WORKER_SERVICE_TOKEN is required when SCAN_BACKEND=worker")


def sign_worker_request(*, job_id: str, ts: str, body: bytes = b"") -> str:
    """HMAC-SHA256 over ``job_id:ts:sha256(body)``."""
    secret = worker_service_token().encode("utf-8")
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{job_id}:{ts}:{body_hash}".encode("utf-8")
    return hmac.new(secret, msg, hashlib.sha256).hexdigest()


def verify_worker_request(
    *,
    job_id: str,
    authorization: str | None,
    x_cs_timestamp: str | None,
    x_cs_signature: str | None,
    body: bytes = b"",
) -> None:
    """Require HMAC signature. Bare Bearer tokens are not accepted."""
    token = worker_service_token()
    if not token:
        raise HTTPException(503, "Worker auth is not configured.")

    if not x_cs_timestamp or not x_cs_signature:
        raise HTTPException(401, "Worker authentication required.")
    try:
        ts = int(x_cs_timestamp)
    except ValueError as exc:
        raise HTTPException(401, "Invalid worker timestamp.") from exc
    if abs(time.time() - ts) > 600:
        raise HTTPException(401, "Worker timestamp expired.")
    expected = sign_worker_request(job_id=job_id, ts=str(ts), body=body)
    if not hmac.compare_digest(expected, x_cs_signature.strip()):
        raise HTTPException(401, "Invalid worker signature.")


async def require_worker_auth(
    job_id: str,
    authorization: str | None = Header(default=None),
    x_cs_timestamp: str | None = Header(default=None),
    x_cs_signature: str | None = Header(default=None),
) -> None:
    verify_worker_request(
        job_id=job_id,
        authorization=authorization,
        x_cs_timestamp=x_cs_timestamp,
        x_cs_signature=x_cs_signature,
        body=b"",
    )
