"""Optional Sentry with path/text scrubbing — never send fact values or snippets."""

from __future__ import annotations

import re
from typing import Any

from app.config import settings

_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']{3,}")
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")

_SENSITIVE_KEYS = {
    "value",
    "display",
    "quote",
    "snippet",
    "explanation",
    "title",
    "text_excerpt",
    "claim_quote",
    "evidence_quote",
    "why_suspicious",
    "canonical_name",
    "aliases",
    "description",
}


def init_sentry(*, service: str) -> None:
    dsn = (settings.sentry_dsn or "").strip()
    if not dsn:
        return
    try:
        import sentry_sdk
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration
    except ImportError:
        return

    def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
        return _scrub_event(event)

    sentry_sdk.init(
        dsn=dsn,
        environment=settings.sentry_environment or "development",
        release=f"{settings.app_name}@{settings.app_version}",
        traces_sample_rate=0.05,
        send_default_pii=False,
        before_send=_before_send,
        integrations=[StarletteIntegration(), FastApiIntegration()],
    )
    sentry_sdk.set_tag("service", service)
    support = cases_db_support_code()
    if support:
        sentry_sdk.set_user({"id": support})


def cases_db_support_code() -> str | None:
    try:
        from app import cases_db

        code = cases_db.get_setting("cloud_support_code")
        return code or None
    except Exception:
        return None


def _scrub_value(value: Any) -> Any:
    if isinstance(value, (bytes, bytearray, memoryview)):
        return "[redacted]"
    if isinstance(value, str):
        value = _PATH_RE.sub("[path]", value)
        value = _EMAIL_RE.sub("[email]", value)
        if len(value) > 500:
            return value[:500] + "…"
        return value
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            key = str(k)
            if key.lower() in _SENSITIVE_KEYS or key.endswith("_enc") or "snippet" in key.lower():
                out[k] = "[redacted]"
            else:
                out[k] = _scrub_value(v)
        return out
    if isinstance(value, list):
        return [_scrub_value(v) for v in value[:50]]
    return value


def _scrub_event(event: dict[str, Any]) -> dict[str, Any]:
    # Drop request bodies entirely — may contain uploads / fact payloads.
    req = event.get("request")
    if isinstance(req, dict):
        # Multipart upload bodies must not ride along on request.data, body, or files.
        for key in ("data", "body", "files", "cookies"):
            if key in req:
                req[key] = "[redacted]"
        event["request"] = _scrub_value(req)
    for key in ("breadcrumbs", "extra", "contexts", "exception"):
        if key in event:
            event[key] = _scrub_value(event[key])
    if "message" in event and isinstance(event["message"], str):
        event["message"] = _scrub_value(event["message"])
    return event
