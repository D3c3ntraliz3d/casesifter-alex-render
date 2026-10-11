"""Gemini Files API helpers — stream PDFs from disk (no base64), then delete."""

from __future__ import annotations

import http.client
import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

from app.config import settings

logger = logging.getLogger(__name__)

_UPLOAD_START = "https://generativelanguage.googleapis.com/upload/v1beta/files"
_FILES_HOST = "generativelanguage.googleapis.com"
_FILES_BASE = "https://generativelanguage.googleapis.com/v1beta"
_CHUNK = 1024 * 1024
_MAX_ATTEMPTS = 5


def _api_key() -> str:
    return (settings.gemini_api_key or "").strip()


def _job_prefix() -> str:
    from app.gemini_cancel import current_job_id

    job = current_job_id() or ""
    return job[:8] if job else "-"


def _retry_delay(attempt: int, headers) -> float:
    retry_after = headers.get("Retry-After") if headers else None
    try:
        delay = float(retry_after) if retry_after else (1.5 * (2**attempt))
    except (TypeError, ValueError):
        delay = 1.5 * (2**attempt)
    return min(60.0, max(0.5, delay))


def _urlopen_with_retries(
    request: urllib.request.Request,
    timeout: float,
    *,
    note_failures: bool = True,
):
    """urlopen with 429 backoff (same schedule as ``_post_gemini``); 402/403 → billing."""
    from app.gemini_review_state import note_ai_review_failure, reason_for_http_status

    job_prefix = _job_prefix()
    last_err: Exception | None = None
    last_reason = "error"
    for attempt in range(_MAX_ATTEMPTS):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as exc:
            logger.warning(
                "gemini files non-200 status=%s attempt=%s/%s job=%s",
                exc.code,
                attempt + 1,
                _MAX_ATTEMPTS,
                job_prefix,
            )
            detail = exc.read().decode("utf-8", errors="replace")[:300]
            reason = reason_for_http_status(exc.code)
            if exc.code in (402, 403):
                logger.error(
                    "gemini files non-retryable status=%s attempt=%s/%s job=%s",
                    exc.code,
                    attempt + 1,
                    _MAX_ATTEMPTS,
                    job_prefix,
                )
                if note_failures:
                    note_ai_review_failure(reason)
                raise RuntimeError(f"Gemini Files HTTP {exc.code}: {detail}") from exc
            if exc.code in (429, 500, 502, 503, 504) and attempt + 1 < _MAX_ATTEMPTS:
                time.sleep(_retry_delay(attempt, exc.headers))
                last_err = RuntimeError(f"Gemini Files HTTP {exc.code}: {detail}")
                last_reason = reason
                continue
            if note_failures:
                note_ai_review_failure(reason)
            raise RuntimeError(f"Gemini Files HTTP {exc.code}: {detail}") from exc
        except urllib.error.URLError as exc:
            if note_failures:
                note_ai_review_failure("error")
            raise RuntimeError(f"Could not reach Gemini Files API: {exc.reason}") from exc
    if last_err is not None:
        if note_failures:
            note_ai_review_failure(last_reason)
        raise last_err
    if note_failures:
        note_ai_review_failure("error")
    raise RuntimeError("Gemini Files request failed after retries.")


def upload_file_streaming(path: Path, *, mime_type: str = "application/pdf") -> dict[str, Any]:
    """Resumable upload: stream file bytes from disk in 1 MiB chunks (never base64)."""
    from app.gemini_review_state import note_ai_review_failure

    size = int(path.stat().st_size)
    job_prefix = _job_prefix()
    logger.info(
        "gemini files upload start job=%s size_mb=%.2f",
        job_prefix,
        size / (1024 * 1024),
    )

    start_body = json.dumps({"file": {"display_name": (path.name or "exhibit.pdf")[:200]}}).encode(
        "utf-8"
    )
    start_req = urllib.request.Request(
        _UPLOAD_START,
        data=start_body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Goog-Upload-Protocol": "resumable",
            "X-Goog-Upload-Command": "start",
            "X-Goog-Upload-Header-Content-Length": str(size),
            "X-Goog-Upload-Header-Content-Type": mime_type,
            "x-goog-api-key": _api_key(),
        },
    )
    with _urlopen_with_retries(start_req, settings.gemini_timeout_s) as resp:
        upload_url = resp.headers.get("X-Goog-Upload-URL") or resp.headers.get("x-goog-upload-url")
    if not upload_url:
        raise RuntimeError("Gemini Files API did not return an upload URL.")

    parsed = urllib.parse.urlparse(upload_url)
    path_q = parsed.path + (f"?{parsed.query}" if parsed.query else "")
    timeout = max(120, int(settings.gemini_document_timeout_s))
    conn = http.client.HTTPSConnection(parsed.hostname or _FILES_HOST, timeout=timeout)
    try:
        conn.putrequest("POST", path_q)
        conn.putheader("Content-Length", str(size))
        conn.putheader("X-Goog-Upload-Offset", "0")
        conn.putheader("X-Goog-Upload-Command", "upload, finalize")
        conn.putheader("x-goog-api-key", _api_key())
        conn.endheaders()
        with open(path, "rb") as handle:
            while True:
                block = handle.read(_CHUNK)
                if not block:
                    break
                conn.send(block)
        resp = conn.getresponse()
        raw = resp.read()
        if resp.status in (402, 403):
            detail = raw.decode("utf-8", errors="replace")[:300]
            logger.error(
                "gemini files upload non-retryable status=%s job=%s",
                resp.status,
                job_prefix,
            )
            note_ai_review_failure("billing")
            raise RuntimeError(f"Gemini Files upload HTTP {resp.status}: {detail}")
        if resp.status == 429:
            detail = raw.decode("utf-8", errors="replace")[:300]
            note_ai_review_failure("rate_limited")
            raise RuntimeError(f"Gemini Files upload HTTP 429: {detail}")
        if resp.status >= 400:
            detail = raw.decode("utf-8", errors="replace")[:300]
            note_ai_review_failure("error")
            raise RuntimeError(f"Gemini Files upload HTTP {resp.status}: {detail}")
        body = json.loads(raw.decode("utf-8"))
    finally:
        conn.close()

    file_obj = body.get("file") if isinstance(body.get("file"), dict) else body
    if not isinstance(file_obj, dict) or not (file_obj.get("uri") or file_obj.get("name")):
        raise RuntimeError("Gemini Files API upload returned no file uri/name.")
    logger.info(
        "gemini files upload done job=%s size_mb=%.2f name=%s",
        job_prefix,
        size / (1024 * 1024),
        str(file_obj.get("name") or "")[:48],
    )
    return file_obj


def wait_file_active(file_obj: dict[str, Any], *, timeout_s: float = 120.0) -> dict[str, Any]:
    """Poll until state is ACTIVE (or FAILED / timeout)."""
    name = str(file_obj.get("name") or "")
    if not name:
        raise RuntimeError("Gemini file missing name.")
    if str(file_obj.get("state") or "") == "ACTIVE":
        return file_obj
    deadline = time.monotonic() + max(5.0, timeout_s)
    latest = file_obj
    while time.monotonic() < deadline:
        state = str(latest.get("state") or "")
        if state == "ACTIVE":
            return latest
        if state == "FAILED":
            raise RuntimeError("Gemini file processing failed.")
        time.sleep(0.75)
        latest = get_file(name)
    raise RuntimeError("Gemini file did not become ACTIVE in time.")


def get_file(name: str) -> dict[str, Any]:
    name = name if name.startswith("files/") else f"files/{name}"
    req = urllib.request.Request(
        f"{_FILES_BASE}/{name}",
        method="GET",
        headers={"x-goog-api-key": _api_key()},
    )
    with _urlopen_with_retries(req, settings.gemini_timeout_s) as resp:
        return json.loads(resp.read().decode("utf-8"))


def delete_file(file_obj: dict[str, Any] | str | None) -> None:
    """Best-effort delete. Logs job prefix only — never file content."""
    if not file_obj:
        return
    name = file_obj if isinstance(file_obj, str) else str(file_obj.get("name") or "")
    if not name:
        return
    name = name if name.startswith("files/") else f"files/{name}"
    job_prefix = _job_prefix()
    try:
        req = urllib.request.Request(
            f"{_FILES_BASE}/{name}",
            method="DELETE",
            headers={"x-goog-api-key": _api_key()},
        )
        with _urlopen_with_retries(
            req, settings.gemini_timeout_s, note_failures=False
        ) as resp:
            resp.read()
        logger.info("gemini files delete ok job=%s name=%s", job_prefix, name[:48])
    except Exception:
        logger.warning(
            "gemini files delete failed job=%s name=%s", job_prefix, name[:48], exc_info=True
        )


def file_part(file_obj: dict[str, Any], *, mime_type: str = "application/pdf") -> dict[str, Any]:
    uri = str(file_obj.get("uri") or "")
    if not uri:
        name = str(file_obj.get("name") or "")
        uri = f"{_FILES_BASE}/{name}" if name else ""
    if not uri:
        raise RuntimeError("Gemini file missing uri.")
    return {"file_data": {"mime_type": mime_type, "file_uri": uri}}
