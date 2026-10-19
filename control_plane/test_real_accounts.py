"""P0-5: argon2 passwords, verify-before-login, reset, logout revoke."""

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
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-p05")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    # Enforce verify-before-login for legacy flow tests in this module.
    monkeypatch.setenv("CASESIFTER_REQUIRE_EMAIL_VERIFY", "true")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import importlib
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-session-secret-p05"
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    yield TestClient(mod.app)
    auth_store.reset_auth_store_for_tests()


def test_wrong_password_rejected(client):
    su = client.post(
        "/v1/signup",
        json={
            "email": "a@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    assert su.status_code == 200, su.text
    token = su.json()["dev_verify_token"]
    assert client.post("/v1/verify-email", json={"token": token}).status_code == 200
    bad = client.post(
        "/v1/login",
        json={"email": "a@example.com", "password": "wrong-password"},
    )
    assert bad.status_code == 401


def test_signup_unverified_until_email_link(client):
    su = client.post(
        "/v1/signup",
        json={
            "email": "b@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    assert su.status_code == 200
    body = su.json()
    assert body.get("email_verified") is False
    assert "token" not in body or not body.get("token")
    blocked = client.post(
        "/v1/login",
        json={"email": "b@example.com", "password": "correct-horse"},
    )
    assert blocked.status_code == 403
    v = client.post("/v1/verify-email", json={"token": body["dev_verify_token"]})
    assert v.status_code == 200
    assert v.json().get("token")
    ok = client.post(
        "/v1/login",
        json={"email": "b@example.com", "password": "correct-horse"},
    )
    assert ok.status_code == 200


def test_demo_mode_signup_skips_verify(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DEV_TOKENS", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-demo-verify-off")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_REQUIRE_EMAIL_VERIFY", "false")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")

    import auth_store
    import importlib
    import main as mod

    auth_store.reset_auth_store_for_tests()
    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-demo-verify-off"
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    http = TestClient(mod.app)
    su = http.post(
        "/v1/signup",
        json={
            "email": "demo-skip@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    assert su.status_code == 200, su.text
    body = su.json()
    assert body.get("email_verified") is True
    assert "dev_verify_token" not in body
    ok = http.post(
        "/v1/login",
        json={"email": "demo-skip@example.com", "password": "correct-horse"},
    )
    assert ok.status_code == 200, ok.text
    auth_store.reset_auth_store_for_tests()


def test_password_reset_flow(client):
    su = client.post(
        "/v1/signup",
        json={
            "email": "c@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    client.post("/v1/verify-email", json={"token": su.json()["dev_verify_token"]})
    req = client.post("/v1/password-reset/request", json={"email": "c@example.com"})
    assert req.status_code == 200
    reset = req.json()["dev_reset_token"]
    conf = client.post(
        "/v1/password-reset/confirm",
        json={"token": reset, "password": "new-correct-horse"},
    )
    assert conf.status_code == 200
    assert (
        client.post(
            "/v1/login",
            json={"email": "c@example.com", "password": "correct-horse"},
        ).status_code
        == 401
    )
    assert (
        client.post(
            "/v1/login",
            json={"email": "c@example.com", "password": "new-correct-horse"},
        ).status_code
        == 200
    )


def test_logout_revokes_old_token(client):
    su = client.post(
        "/v1/signup",
        json={
            "email": "d@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    tok = client.post("/v1/verify-email", json={"token": su.json()["dev_verify_token"]}).json()[
        "token"
    ]
    assert client.get("/v1/entitlement", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.post("/v1/logout", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.get("/v1/entitlement", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
