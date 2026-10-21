"""Production HTTP surface: hide API docs and set hardened response headers.

Transport: TLS is terminated at the edge (Render / reverse proxy). We require
TLS 1.2+ (prefer 1.3) at that edge and emit HSTS so browsers refuse cleartext.
"""

from __future__ import annotations

import os

from fastapi import FastAPI
from fastapi.responses import JSONResponse

# 2 years + subdomains. Enable HSTS preload only after domains are listed.
HSTS_VALUE = "max-age=63072000; includeSubDomains; preload"


def docs_kwargs(app_env: str | None) -> dict:
    """FastAPI docs stay on in development and off in production."""
    if (app_env or "").strip().lower() == "production":
        return {"docs_url": None, "redoc_url": None, "openapi_url": None}
    return {
        "docs_url": "/docs",
        "redoc_url": "/redoc",
        "openapi_url": "/openapi.json",
    }


def _production() -> bool:
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def _force_hsts() -> bool:
    return (os.environ.get("FORCE_HSTS") or "").strip().lower() in {"1", "true", "yes"}


def _request_is_https(request) -> bool:
    proto = (request.headers.get("x-forwarded-proto") or "").split(",")[0].strip().lower()
    if proto == "https":
        return True
    if proto == "http":
        return False
    url = getattr(request, "url", None)
    return bool(url and getattr(url, "scheme", "") == "https")


def apply_security_headers(response, *, https: bool = False, production: bool | None = None) -> None:
    """Mutate response headers in place (shared by middleware and tests)."""
    prod = _production() if production is None else production
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
    # APIs should not be framed or mixed into foreign documents.
    response.headers.setdefault(
        "Content-Security-Policy",
        "default-src 'none'; frame-ancestors 'none'; base-uri 'none'",
    )
    response.headers.setdefault("Cache-Control", "no-store")
    if (prod or _force_hsts()) and https:
        response.headers["Strict-Transport-Security"] = HSTS_VALUE


def install_api_headers(app: FastAPI) -> None:
    @app.middleware("http")
    async def _api_security_headers(request, call_next):
        response = await call_next(request)
        apply_security_headers(
            response,
            https=_request_is_https(request),
            production=_production(),
        )
        return response


def _referer_origin(referer: str) -> str:
    raw = (referer or "").strip()
    if not raw:
        return ""
    try:
        from urllib.parse import urlparse

        parsed = urlparse(raw)
        if not parsed.scheme or not parsed.netloc:
            return ""
        return f"{parsed.scheme}://{parsed.netloc}".rstrip("/")
    except Exception:
        return ""


def origin_blocked(
    origin: str,
    allowed: set[str],
    *,
    production: bool,
    referer: str = "",
    sec_fetch_site: str = "",
) -> bool:
    """Cookie-authenticated unsafe requests must come from an allowed origin.

    Same-origin SPA rewrites (Render static → analysis) sometimes omit or strip
    Origin while still forwarding the session cookie. Treat empty Origin as OK
    only when Sec-Fetch-Site is same-origin or Referer host is allowlisted.
    A present but disallowed Origin always blocks.
    """
    cleaned = (origin or "").rstrip("/")
    if cleaned in allowed:
        return False
    if cleaned:
        return True
    # Missing Origin
    if not production:
        return False
    if (sec_fetch_site or "").strip().lower() == "same-origin":
        return False
    ref_origin = _referer_origin(referer)
    if ref_origin and ref_origin in allowed:
        return False
    return True


def csrf_block_response() -> JSONResponse:
    return JSONResponse({"detail": "Cross-site request blocked."}, status_code=403)
