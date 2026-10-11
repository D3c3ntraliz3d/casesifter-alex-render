"""HTTP client: worker → analysis internal API."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import settings
from app.worker_auth import sign_worker_request

logger = logging.getLogger(__name__)


def _base() -> str:
    base = (settings.worker_callback_base_url or "http://127.0.0.1:8000").strip().rstrip("/")
    # Render's blueprint `fromService ... property: hostport` yields "name:port"
    # (private network, no scheme). Internal traffic is plain HTTP.
    if "://" not in base:
        base = f"http://{base}"
    return base


def _post(job_id: str, path: str, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    ts = str(int(time.time()))
    sig = sign_worker_request(job_id=job_id, ts=ts, body=body)
    url = f"{_base()}{path}"
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-CS-Timestamp": ts,
            "X-CS-Signature": sig,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"callback HTTP {exc.code}: {detail}") from exc


def post_progress(job_id: str, **fields: Any) -> None:
    _post(job_id, f"/api/internal/v1/jobs/{job_id}/progress", fields)


RESULT_RETRY_DELAYS_S = (2.0, 5.0, 15.0)


def _is_transient(exc: Exception) -> bool:
    if isinstance(exc, urllib.error.URLError) or isinstance(exc, (TimeoutError, ConnectionError)):
        return True
    msg = str(exc)
    return msg.startswith("callback HTTP 5") or msg.startswith("callback HTTP 429")


def post_result(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    """POST the terminal result, retrying transient failures.

    The result endpoint is idempotent on terminal status, so a retry after a
    lost response is safe. 4xx (auth/validation) errors are not retried.
    """
    path = f"/api/internal/v1/jobs/{job_id}/result"
    for delay in (*RESULT_RETRY_DELAYS_S, None):
        try:
            return _post(job_id, path, payload)
        except Exception as exc:
            if delay is None or not _is_transient(exc):
                raise
            logger.warning(
                "result POST transient failure job=%s retry_in=%ss: %s",
                job_id[:8],
                delay,
                str(exc)[:200],
            )
            time.sleep(delay)
    raise RuntimeError("unreachable")
