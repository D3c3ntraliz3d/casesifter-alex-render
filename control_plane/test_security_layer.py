"""Cookie flags, admin header, mailer silence, docs lockdown, password length."""

from __future__ import annotations

import importlib
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
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-layer")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")
    monkeypatch.setenv("CONTROL_ADMIN_TOKEN", "admin-secret-value")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-session-secret-layer"
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    yield TestClient(mod.app), mod
    auth_store.reset_auth_store_for_tests()


def _verified(client: TestClient) -> None:
    su = client.post(
        "/v1/signup",
        json={"email": "layer@example.com", "password": "correct-horse", "accepted_terms": True},
    )
    assert su.status_code == 200, su.text
    body = su.json()
    assert "token" not in body or not body.get("token")
    token = body.get("dev_verify_token")
    if token:
        verified = client.post("/v1/verify-email", json={"token": token})
        assert verified.status_code == 200, verified.text
    else:
        assert body.get("email_verified") is True


def test_login_sets_httponly_cookie_and_keeps_dev_token(client):
    http, _mod = client
    _verified(http)
    logged = http.post("/v1/login", json={"email": "layer@example.com", "password": "correct-horse"})
    assert logged.status_code == 200, logged.text
    header = logged.headers.get("set-cookie") or ""
    assert "cs_session=" in header
    assert "httponly" in header.lower()
    assert logged.json().get("token")
    assert logged.json().get("analysis_code")


def test_production_login_returns_token_with_accept_bearer(tmp_path, monkeypatch):
    """Cookie-blocked browsers request the opaque session token via Accept-Bearer."""
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("SESSION_SECRET", "prod-session-secret-bearer")
    monkeypatch.delenv("AUTH_DEV_TOKENS", raising=False)
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_REQUIRE_EMAIL_VERIFY", "false")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "prod-session-secret-bearer"
    http = TestClient(mod.app)
    su = http.post(
        "/v1/signup",
        json={"email": "bearer@example.com", "password": "correct-horse", "accepted_terms": True},
    )
    assert su.status_code == 200, su.text
    assert not su.json().get("token")

    no_hdr = http.post(
        "/v1/login",
        json={"email": "bearer@example.com", "password": "correct-horse"},
    )
    assert no_hdr.status_code == 200, no_hdr.text
    assert not no_hdr.json().get("token")
    assert "cs_session=" in (no_hdr.headers.get("set-cookie") or "")

    with_hdr = http.post(
        "/v1/login",
        json={"email": "bearer@example.com", "password": "correct-horse"},
        headers={"X-Casesifter-Accept-Bearer": "1"},
    )
    assert with_hdr.status_code == 200, with_hdr.text
    assert with_hdr.json().get("token")
    assert with_hdr.json().get("analysis_code")
    auth_store.reset_auth_store_for_tests()


def test_short_password_rejected(client):
    http, _mod = client
    res = http.post(
        "/v1/login",
        json={"email": "layer@example.com", "password": "short"},
    )
    assert res.status_code == 422


def test_admin_token_header_not_body(client):
    http, _mod = client
    missing = http.post("/v1/trial", json={"email": "grant@example.com", "admin_token": "admin-secret-value"})
    assert missing.status_code == 403
    ok = http.post(
        "/v1/trial",
        json={"email": "grant@example.com"},
        headers={"X-Admin-Token": "admin-secret-value"},
    )
    assert ok.status_code == 200, ok.text
    assert "admin-secret-value" not in ok.text


def test_mailer_does_not_echo_verify_token(client, monkeypatch):
    http, mod = client
    sent = {}

    def fake_send(to_addr, subject, text):
        sent["to"] = to_addr
        sent["text"] = text
        return True

    monkeypatch.setenv("CASESIFTER_REQUIRE_EMAIL_VERIFY", "true")
    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-session-secret-layer"
    http = TestClient(mod.app)
    monkeypatch.setattr(mod.mailer, "mailer_configured", lambda: True)
    monkeypatch.setattr(mod.mailer, "send_auth_email", fake_send)
    res = http.post(
        "/v1/signup",
        json={"email": "mailed@example.com", "password": "correct-horse", "accepted_terms": True},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert "dev_verify_token" not in body
    assert "token" not in body
    assert sent["to"] == "mailed@example.com"
    assert "verify_token=" in sent["text"]
    assert sent["text"].split("verify_token=", 1)[1].split()[0] not in res.text


def test_production_hides_docs_and_requires_mail(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("SESSION_SECRET", "prod-session-secret")
    monkeypatch.delenv("AUTH_DEV_TOKENS", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.delenv("SMTP_FROM", raising=False)
    monkeypatch.delenv("EMAIL_FROM", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_REQUIRE_EMAIL_VERIFY", "true")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "prod-session-secret"
    http = TestClient(mod.app)
    assert http.get("/docs").status_code == 404
    assert http.get("/openapi.json").status_code == 404
    signup = http.post(
        "/v1/signup",
        json={"email": "prod@example.com", "password": "correct-horse", "accepted_terms": True},
    )
    assert signup.status_code == 503
    assert "dev_verify_token" not in signup.text
    auth_store.reset_auth_store_for_tests()


def test_resend_counts_as_configured_mail(monkeypatch):
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("SMTP_FROM", "CaseSifter <noreply@example.com>")
    import mailer

    assert mailer.mailer_configured() is True


def test_production_signup_rolls_back_when_send_fails(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path / 'auth.db'}")
    monkeypatch.setenv("SESSION_SECRET", "prod-session-secret")
    monkeypatch.delenv("AUTH_DEV_TOKENS", raising=False)
    monkeypatch.delenv("SMTP_HOST", raising=False)
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("SMTP_FROM", "CaseSifter <noreply@example.com>")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_REQUIRE_EMAIL_VERIFY", "true")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "prod-session-secret"
    monkeypatch.setattr(mod.mailer, "send_auth_email", lambda *_a, **_k: False)
    http = TestClient(mod.app)
    body = {"email": "prod@example.com", "password": "correct-horse", "accepted_terms": True}
    first = http.post("/v1/signup", json=body)
    assert first.status_code == 503
    assert "dev_verify_token" not in first.text
    second = http.post("/v1/signup", json=body)
    assert second.status_code == 503
    assert "already exists" not in second.text.lower()
    auth_store.reset_auth_store_for_tests()
