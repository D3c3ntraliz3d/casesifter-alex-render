"""P0 blocker: never return verify/reset tokens unless AUTH_DEV_TOKENS=true."""

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
def client_default(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("AUTH_DEV_TOKENS", raising=False)
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.delenv("RESEND_API_KEY", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-no-dev-tokens")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("CASESIFTER_SINGLE_USER_EMAIL", SINGLE_EMAIL)
    monkeypatch.setenv("CASESIFTER_SINGLE_USER_PASSWORD", SINGLE_PASSWORD)

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import importlib
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-no-dev-tokens"
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    mod._ensure_single_user()
    yield TestClient(mod.app)
    auth_store.reset_auth_store_for_tests()


def test_signup_closed_and_reset_never_return_tokens_by_default(client_default):
    c = client_default
    su = c.post(
        "/v1/signup",
        json={
            "email": "victim@example.com",
            "password": "correct-horse",
            "accepted_terms": True,
        },
    )
    assert su.status_code == 403
    assert "dev_verify_token" not in su.json()

    reset = c.post("/v1/password-reset/request", json={"email": SINGLE_EMAIL})
    assert reset.status_code == 200
    assert "dev_reset_token" not in reset.json()
    assert "dev_verify_token" not in reset.json()
