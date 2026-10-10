"""Optional Sentry with strict allowlist scrubbing — never send case data."""

from __future__ import annotations

import re
from typing import Any
from urllib.parse import urlsplit, urlunsplit

from app.config import settings

_HEX32_RE = re.compile(r"\b[a-f0-9]{32}\b", re.I)
_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}")
_PATH_RE = re.compile(r"(?:[A-Za-z]:\\|/)[^\s\"']{3,}")
_UUID_RE = re.compile(
    r"\b[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}\b",
    re.I,
)

# Route-ish path segments that look like opaque IDs → template placeholders.
_ID_SEGMENT_RE = re.compile(
    r"(?<=/)(?:[a-f0-9]{32}|[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})(?=/|$)",
    re.I,
)


def init_sentry(*, service: str) -> None:
    # Prefer a dedicated Python/backend DSN so events are not mixed into javascript-react.
    dsn = (
        (getattr(settings, "sentry_backend_dsn", None) or "").strip()
        or (settings.sentry_dsn or "").strip()
    )
    if not dsn:
        return
    try:
        import sentry_sdk
    except ImportError:
        return

    def _before_send(event: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
        return _scrub_event(event)

    def _before_breadcrumb(crumb: dict[str, Any], _hint: dict[str, Any]) -> dict[str, Any] | None:
        return _scrub_breadcrumb(crumb)

    init_kwargs: dict[str, Any] = {
        "dsn": dsn,
        "environment": settings.sentry_environment or "development",
        "release": f"{settings.app_name}@{settings.app_version}",
        "traces_sample_rate": 0.05,
        "send_default_pii": False,
        "attach_stacktrace": False,
        "include_local_variables": False,
        "max_request_body_size": "never",
        "before_send": _before_send,
        "before_breadcrumb": _before_breadcrumb,
    }
    try:
        from sentry_sdk.integrations.fastapi import FastApiIntegration
        from sentry_sdk.integrations.starlette import StarletteIntegration

        init_kwargs["integrations"] = [StarletteIntegration(), FastApiIntegration()]
    except ImportError:
        pass

    sentry_sdk.init(**init_kwargs)
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


def _scrub_string(value: str) -> str:
    value = _PATH_RE.sub("[path]", value)
    value = _EMAIL_RE.sub("[email]", value)
    value = _HEX32_RE.sub("[id]", value)
    value = _UUID_RE.sub("[id]", value)
    if len(value) > 300:
        return value[:300] + "…"
    return value


def route_template(url_or_path: str) -> str:
    """Strip query/fragment and replace opaque ID segments with {id}."""
    if not url_or_path:
        return ""
    try:
        parts = urlsplit(url_or_path)
        path = parts.path or url_or_path
    except Exception:
        path = url_or_path.split("?", 1)[0].split("#", 1)[0]
    path = _ID_SEGMENT_RE.sub("{id}", path)
    path = _HEX32_RE.sub("{id}", path)
    return path


def _scrub_frame(frame: dict[str, Any]) -> dict[str, Any]:
    return {
        "filename": _scrub_string(str(frame.get("filename") or "")),
        "function": str(frame.get("function") or "")[:120],
        "lineno": frame.get("lineno"),
        "module": str(frame.get("module") or "")[:200] if frame.get("module") else None,
        # Explicitly drop vars / pre_context / post_context / context_line content.
    }


def _scrub_exception(exc: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {"type": str(exc.get("type") or "Error")[:120]}
    # Drop value (may contain filenames / IDs); keep type only.
    stack = exc.get("stacktrace")
    if isinstance(stack, dict):
        frames = stack.get("frames")
        if isinstance(frames, list):
            out["stacktrace"] = {"frames": [_scrub_frame(f) for f in frames if isinstance(f, dict)]}
    return out


def _scrub_breadcrumb(crumb: dict[str, Any]) -> dict[str, Any] | None:
    if not isinstance(crumb, dict):
        return None
    category = str(crumb.get("category") or "")
    level = str(crumb.get("level") or "info")
    data = crumb.get("data") if isinstance(crumb.get("data"), dict) else {}
    url = data.get("url") or data.get("to") or ""
    out: dict[str, Any] = {
        "category": category[:80],
        "level": level[:20],
    }
    if url:
        out["data"] = {"url": route_template(str(url))}
    return out


def _scrub_request(req: dict[str, Any]) -> dict[str, Any]:
    url = str(req.get("url") or "")
    headers_in = req.get("headers") if isinstance(req.get("headers"), dict) else {}
    ua = ""
    for k, v in headers_in.items():
        if str(k).lower() == "user-agent":
            ua = str(v)[:200]
            break
    out: dict[str, Any] = {
        "url": route_template(url),
        "method": str(req.get("method") or "")[:16],
    }
    if ua:
        out["headers"] = {"User-Agent": ua}
    # Drop query_string, data, body, cookies, env, etc.
    return out


def _scrub_contexts(contexts: dict[str, Any]) -> dict[str, Any]:
    keep = {}
    for key in ("runtime", "os"):
        if key in contexts and isinstance(contexts[key], dict):
            # Shallow copy of non-string-heavy fields only.
            slim = {}
            for k, v in contexts[key].items():
                if isinstance(v, (str, int, float, bool)) or v is None:
                    slim[k] = _scrub_string(v) if isinstance(v, str) else v
            keep[key] = slim
    return keep


def _scrub_event(event: dict[str, Any]) -> dict[str, Any]:
    """Allowlist: keep only safe diagnostic fields."""
    out: dict[str, Any] = {}

    if "level" in event:
        out["level"] = str(event["level"])[:20]
    if "tags" in event and isinstance(event["tags"], dict):
        out["tags"] = {
            str(k)[:64]: _scrub_string(str(v))[:120]
            for k, v in list(event["tags"].items())[:40]
        }
    elif "tags" in event and isinstance(event["tags"], list):
        # Sentry sometimes uses [["k","v"], ...]
        out["tags"] = {
            str(pair[0])[:64]: _scrub_string(str(pair[1]))[:120]
            for pair in event["tags"][:40]
            if isinstance(pair, (list, tuple)) and len(pair) >= 2
        }
    if "release" in event:
        out["release"] = _scrub_string(str(event["release"]))[:120]
    if "environment" in event:
        out["environment"] = str(event["environment"])[:64]
    if "transaction" in event:
        out["transaction"] = route_template(str(event["transaction"]))

    if "exception" in event and isinstance(event["exception"], dict):
        values = event["exception"].get("values")
        if isinstance(values, list):
            out["exception"] = {
                "values": [_scrub_exception(v) for v in values if isinstance(v, dict)]
            }

    if "request" in event and isinstance(event["request"], dict):
        out["request"] = _scrub_request(event["request"])

    if "contexts" in event and isinstance(event["contexts"], dict):
        out["contexts"] = _scrub_contexts(event["contexts"])

    if "breadcrumbs" in event:
        crumbs = event["breadcrumbs"]
        values = crumbs.get("values") if isinstance(crumbs, dict) else crumbs
        if isinstance(values, list):
            scrubbed = [_scrub_breadcrumb(c) for c in values if isinstance(c, dict)]
            out["breadcrumbs"] = {"values": [c for c in scrubbed if c]}

    # Drop: extra, user.ip_address, modules, server_name, message bodies with secrets.
    if "user" in event and isinstance(event["user"], dict):
        uid = event["user"].get("id")
        if uid is not None:
            out["user"] = {"id": _scrub_string(str(uid))[:64]}

    if "message" in event and isinstance(event["message"], str):
        out["message"] = _scrub_string(event["message"])

    # Preserve event_id / timestamp for correlation only.
    for key in ("event_id", "timestamp", "platform"):
        if key in event:
            out[key] = event[key]

    return out
