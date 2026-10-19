"""Hide API docs in production and set hardened API headers.

TLS 1.2+ (prefer 1.3) is terminated at the edge; HSTS is emitted on HTTPS.
"""

from __future__ import annotations

import os

from fastapi import FastAPI

HSTS_VALUE = "max-age=63072000; includeSubDomains; preload"


def docs_kwargs(app_env: str | None) -> dict:
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
    prod = _production() if production is None else production
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = (
        "camera=(), microphone=(), geolocation=(), payment=(), usb=(), interest-cohort=()"
    )
    response.headers["Cross-Origin-Opener-Policy"] = "same-origin"
    # cross-origin: SPA on app.* must read credentialed JSON from control/analysis.
    # same-origin CORP can break cross-site fetch in stricter / embedded browsers.
    response.headers["Cross-Origin-Resource-Policy"] = "cross-origin"
    response.headers["X-Permitted-Cross-Domain-Policies"] = "none"
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
