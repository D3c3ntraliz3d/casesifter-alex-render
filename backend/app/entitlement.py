"""Hybrid entitlement: local license / open mode, optional cloud control plane stub."""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app import cases_db
from app.config import settings


def entitlement_status() -> dict[str, Any]:
    mode = settings.entitlement_mode
    share = cases_db.get_setting(
        "share_anonymized_outcomes",
        "true" if settings.share_anonymized_outcomes else "false",
    )
    base = {
        "mode": mode,
        "share_anonymized_outcomes": share == "true",
        "cloud_control_url": settings.cloud_control_url,
        "support_code": cases_db.get_setting("cloud_support_code") or None,
        "cohort": cases_db.get_setting("cloud_cohort") or None,
        "hasSession": bool(cases_db.get_setting("cloud_session_token")),
    }

    if mode == "open":
        return {
            **base,
            "active": True,
            "plan": "local-dev",
            "message": "Open mode — scans unlocked for local use.",
        }

    if mode == "license":
        key = (settings.license_key or cases_db.get_setting("license_key") or "").strip()
        active = bool(key) and len(key) >= 8
        return {
            **base,
            "active": active,
            "plan": "license" if active else None,
            "message": "License key accepted." if active else "Enter a valid license key to scan.",
            "license_present": bool(key),
        }

    token = cases_db.get_setting("cloud_session_token")
    if not token:
        return {
            **base,
            "active": False,
            "plan": None,
            "message": "Sign in to activate your monthly subscription.",
        }
    remote = _probe_cloud_entitlement(token)
    if remote is None:
        return {
            **base,
            "active": True,
            "plan": "cloud-cached",
            "message": "Using cached session; cloud control plane unreachable.",
            "cached": True,
            "email": cases_db.get_setting("cloud_email"),
        }
    return {**base, **remote}


def require_scan_allowed() -> dict[str, Any]:
    status = entitlement_status()
    if not status.get("active"):
        raise PermissionError(status.get("message") or "Subscription inactive.")
    return status


def save_license_key(key: str) -> dict[str, Any]:
    cases_db.set_setting("license_key", key.strip())
    return entitlement_status()


def cloud_login(email: str, password: str) -> dict[str, Any]:
    """Stub cloud login. Production hits the control plane; local mints a session."""
    email = email.strip().lower()
    if not email or "@" not in email or not password:
        raise ValueError("Email and password are required.")

    if settings.cloud_control_url:
        payload = json.dumps({"email": email, "password": password}).encode("utf-8")
        req = urllib.request.Request(
            settings.cloud_control_url.rstrip("/") + "/v1/login",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        try:
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read().decode("utf-8"))
            token = data.get("token") or data.get("access_token")
            if not token:
                raise RuntimeError("Cloud login returned no token.")
            cases_db.set_setting("cloud_session_token", str(token))
            cases_db.set_setting("cloud_email", email)
            ent = data.get("entitlement") or data
            _persist_cloud_identity(ent, email)
            return entitlement_status()
        except urllib.error.URLError as exc:
            raise RuntimeError(f"Could not reach cloud control plane: {exc.reason}") from exc

    cases_db.set_setting("cloud_session_token", f"local-stub:{email}")
    cases_db.set_setting("cloud_email", email)
    return entitlement_status()


def cloud_logout() -> dict[str, Any]:
    cases_db.set_setting("cloud_session_token", "")
    return entitlement_status()


def set_share_outcomes(enabled: bool) -> dict[str, Any]:
    cases_db.set_setting("share_anonymized_outcomes", "true" if enabled else "false")
    return entitlement_status()


def _probe_cloud_entitlement(token: str) -> dict[str, Any] | None:
    if not settings.cloud_control_url:
        return {
            "active": True,
            "plan": "stub-monthly",
            "message": "Local cloud stub — subscription treated as active.",
            "email": cases_db.get_setting("cloud_email"),
        }
    req = urllib.request.Request(
        settings.cloud_control_url.rstrip("/") + "/v1/entitlement",
        method="GET",
        headers={"Authorization": f"Bearer {token}"},
    )
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        _persist_cloud_identity(data, data.get("email") or cases_db.get_setting("cloud_email") or "")
        return {
            "active": bool(data.get("active")),
            "plan": data.get("plan"),
            "message": data.get("message") or "",
            "email": data.get("email") or cases_db.get_setting("cloud_email"),
            "support_code": data.get("support_code") or cases_db.get_setting("cloud_support_code"),
            "cohort": data.get("cohort") or cases_db.get_setting("cloud_cohort"),
            "hasSession": bool(token),
        }
    except Exception:
        return None


def _persist_cloud_identity(data: dict[str, Any], email: str) -> None:
    if email:
        cases_db.set_setting("cloud_email", str(email).lower())
    code = data.get("support_code")
    if code:
        cases_db.set_setting("cloud_support_code", str(code))
    uid = data.get("user_id")
    if uid:
        cases_db.set_setting("cloud_user_id", str(uid))
    cohort = data.get("cohort")
    if cohort:
        cases_db.set_setting("cloud_cohort", str(cohort))
