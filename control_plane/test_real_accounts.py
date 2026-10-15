"""P0-5: argon2 passwords, single-user login, reset, logout revoke."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

SINGLE_EMAIL = "admin@libertygroupfunding.com"
SINGLE_PASSWORD = "Casesifter1"


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DEV_TOKENS", "true")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-p05")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_SINGLE_USER_EMAIL", SINGLE_EMAIL)
    monkeypatch.setenv("CASESIFTER_SINGLE_USER_PASSWORD", SINGLE_PASSWORD)
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
    mod._ensure_single_user()
    yield TestClient(mod.app)
    auth_store.reset_auth_store_for_tests()


def test_signup_closed(client):
    su = client.post(
        "/v1/signup",
        json={
            "email": "a@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    assert su.status_code == 403
    assert "join@casesifter.com" in su.json()["detail"]


def test_wrong_password_rejected(client):
    bad = client.post(
        "/v1/login",
        json={"email": SINGLE_EMAIL, "password": "wrong-password"},
    )
    assert bad.status_code == 401


def test_other_email_rejected(client):
    bad = client.post(
        "/v1/login",
        json={"email": "other@example.com", "password": SINGLE_PASSWORD},
    )
    assert bad.status_code == 401


def test_single_user_login(client):
    ok = client.post(
        "/v1/login",
        json={"email": SINGLE_EMAIL, "password": SINGLE_PASSWORD},
    )
    assert ok.status_code == 200, ok.text
    body = ok.json()
    assert body.get("token")
    assert body.get("entitlement", {}).get("active") is True


def test_password_reset_flow(client):
    req = client.post("/v1/password-reset/request", json={"email": SINGLE_EMAIL})
    assert req.status_code == 200
    reset = req.json()["dev_reset_token"]
    conf = client.post(
        "/v1/password-reset/confirm",
        json={"token": reset, "password": "new-correct-horse"},
    )
    assert conf.status_code == 200
    # Allowlist password is re-synced on login via _ensure_single_user.
    assert (
        client.post(
            "/v1/login",
            json={"email": SINGLE_EMAIL, "password": SINGLE_PASSWORD},
        ).status_code
        == 200
    )


def test_logout_revokes_old_token(client):
    tok = client.post(
        "/v1/login",
        json={"email": SINGLE_EMAIL, "password": SINGLE_PASSWORD},
    ).json()["token"]
    assert client.get("/v1/entitlement", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.post("/v1/logout", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert client.get("/v1/entitlement", headers={"Authorization": f"Bearer {tok}"}).status_code == 401
