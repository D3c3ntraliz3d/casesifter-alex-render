"""HTTP client: worker → analysis internal API."""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from typing import Any

from app.config import settings
from app.worker_auth import sign_worker_request, worker_service_token

logger = logging.getLogger(__name__)


def _base() -> str:
    return (settings.worker_callback_base_url or "http://127.0.0.1:8000").rstrip("/")


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
            "Authorization": f"Bearer {worker_service_token()}",
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


def post_result(job_id: str, payload: dict[str, Any]) -> dict[str, Any]:
    return _post(job_id, f"/api/internal/v1/jobs/{job_id}/result", payload)
