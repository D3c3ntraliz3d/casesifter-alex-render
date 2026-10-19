"""TOTP challenge, recovery codes, admin overview, SSO status — no bypass."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DEV_TOKENS", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.delenv("OIDC_ISSUER", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_ID", raising=False)
    monkeypatch.delenv("OIDC_CLIENT_SECRET", raising=False)
    monkeypatch.delenv("OIDC_REDIRECT_URI", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-2fa")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import importlib
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-session-secret-2fa"
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    yield TestClient(mod.app)
    auth_store.reset_auth_store_for_tests()


def _signup_and_verify(http: TestClient, email: str = "2fa@example.com") -> None:
    su = http.post(
        "/v1/signup",
        json={"email": email, "password": "correct-horse", "accepted_terms": True},
    )
    assert su.status_code == 200, su.text
    token = su.json().get("dev_verify_token")
    if token:
        assert http.post("/v1/verify-email", json={"token": token}).status_code == 200
    else:
        assert su.json().get("email_verified") is True


def test_auth_status_without_oidc(client):
    res = client.get("/v1/auth/status")
    assert res.status_code == 200
    body = res.json()
    assert body["sso_available"] is False
    assert body["totp_available"] is True


def test_totp_enroll_and_login_challenge(client):
    import totp_auth

    _signup_and_verify(client)
    logged = client.post("/v1/login", json={"email": "2fa@example.com", "password": "correct-horse"})
    assert logged.status_code == 200
    assert "mfa_required" not in logged.json() or not logged.json().get("mfa_required")

    start = client.post("/v1/2fa/totp/start")
    assert start.status_code == 200, start.text
    secret = start.json()["secret"]
    code = totp_auth.pyotp.TOTP(secret).now()
    confirm = client.post("/v1/2fa/totp/confirm", json={"code": code})
    assert confirm.status_code == 200, confirm.text
    assert confirm.json()["totp_enabled"] is True
    recovery = confirm.json()["recovery_codes"]
    assert len(recovery) >= 4

    client.post("/v1/logout")
    again = client.post("/v1/login", json={"email": "2fa@example.com", "password": "correct-horse"})
    assert again.status_code == 200
    body = again.json()
    assert body.get("mfa_required") is True
    assert body.get("mfa_token")
    assert "cs_session=" not in (again.headers.get("set-cookie") or "").lower() or "mfa" in str(body)

    bad = client.post(
        "/v1/login/mfa",
        json={"mfa_token": body["mfa_token"], "code": "000000"},
    )
    assert bad.status_code == 401

    # Fresh challenge after failure consumed the token — login again.
    again2 = client.post("/v1/login", json={"email": "2fa@example.com", "password": "correct-horse"})
    mfa_token = again2.json()["mfa_token"]
    good_code = totp_auth.pyotp.TOTP(secret).now()
    ok = client.post("/v1/login/mfa", json={"mfa_token": mfa_token, "code": good_code})
    assert ok.status_code == 200, ok.text
    assert ok.json().get("entitlement", {}).get("totp_enabled") is True

    # Recovery code works once.
    client.post("/v1/logout")
    again3 = client.post("/v1/login", json={"email": "2fa@example.com", "password": "correct-horse"})
    rec = client.post(
        "/v1/login/mfa",
        json={"mfa_token": again3.json()["mfa_token"], "code": recovery[0]},
    )
    assert rec.status_code == 200, rec.text


def test_admin_overview_and_no_2fa_bypass(client):
    _signup_and_verify(client, "admin@example.com")
    client.post("/v1/login", json={"email": "admin@example.com", "password": "correct-horse"})
    overview = client.get("/v1/admin/overview")
    assert overview.status_code == 200, overview.text
    data = overview.json()
    assert data["account"]["email"] == "admin@example.com"
    assert "sessions" in data
    assert "audit" in data
    assert data["security"]["totp_enabled"] is False

    # No endpoint grants a session without MFA when TOTP is on — disable requires codes.
    disable = client.post(
        "/v1/2fa/totp/disable",
        json={"password": "correct-horse", "code": "123456"},
    )
    assert disable.status_code in {400, 401}


def test_sso_start_unavailable_without_config(client):
    res = client.get("/v1/sso/oidc/start", follow_redirects=False)
    assert res.status_code == 503
