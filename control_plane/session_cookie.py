"""Host-only session cookie shared in name with the analysis API."""

from __future__ import annotations

import os

from fastapi import Response

COOKIE_NAME = "cs_session"


def cookie_samesite() -> str:
    explicit = (os.environ.get("COOKIE_SAMESITE") or "").strip().lower()
    if explicit in {"lax", "strict", "none"}:
        return explicit
    if (os.environ.get("APP_ENV") or "").strip().lower() == "production":
        return "none"
    return "lax"


def cookie_secure() -> bool:
    if cookie_samesite() == "none":
        return True
    flag = (os.environ.get("COOKIE_SECURE") or "").strip().lower()
    if flag in {"1", "true", "yes"}:
        return True
    return (os.environ.get("APP_ENV") or "").strip().lower() == "production"


def set_session_cookie(response: Response, token: str, *, max_age: int) -> None:
    response.set_cookie(
        COOKIE_NAME,
        token,
        max_age=max(60, int(max_age)),
        httponly=True,
        secure=cookie_secure(),
        samesite=cookie_samesite(),
        path="/",
    )


def clear_session_cookie(response: Response) -> None:
    response.delete_cookie(
        COOKIE_NAME,
        path="/",
        samesite=cookie_samesite(),
        secure=cookie_secure(),
    )
