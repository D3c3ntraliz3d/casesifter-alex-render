"""Smoke: invalid Bearer → 401 Invalid session; entitlement always shares outcomes."""

from __future__ import annotations

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
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setenv("CASESIFTER_INVITE_REQUIRED", "false")
    monkeypatch.setenv("STRIPE_SECRET_KEY", "")
    monkeypatch.setenv("STRIPE_PRICE_ID", "")

    import auth_store

    auth_store.reset_auth_store_for_tests()
    import importlib
    import main as mod

    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-session-secret"
    mod.STRIPE_SECRET = ""
    mod.STRIPE_PRICE_ID = ""
    mod.STORE = tmp_path / "accounts.json"
    mod.DATA = tmp_path
    yield TestClient(mod.app), mod
    auth_store.reset_auth_store_for_tests()


def _verified_token(client, email: str, password: str = "correct-horse") -> str:
    su = client.post(
        "/v1/signup",
        json={"email": email, "password": password, "accepted_terms": True},
    )
    assert su.status_code == 200, su.text
    token = su.json().get("dev_verify_token")
    if token:
        v = client.post("/v1/verify-email", json={"token": token})
        assert v.status_code == 200, v.text
        return v.json()["token"]
    assert su.json().get("email_verified") is True
    logged = client.post("/v1/login", json={"email": email, "password": password})
    assert logged.status_code == 200, logged.text
    return logged.json()["token"]


def test_checkout_rejects_bad_session(client):
    c, _mod = client
    res = c.post(
        "/v1/checkout",
        headers={"Authorization": "Bearer not-a-real-session"},
        json={"trial": True, "quantity": 1},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid session."


def test_entitlement_rejects_bad_session(client):
    c, _mod = client
    res = c.get(
        "/v1/entitlement",
        headers={"Authorization": "Bearer not-a-real-session"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid session."


def test_entitlement_payload_share_always_on(client):
    _c, mod = client
    user = {
        "email": "smoke@example.com",
        "subscription": {},
        "trial_until": 0,
    }
    payload = mod._entitlement_payload(user)
    assert payload["share_anonymized_outcomes"] is True
    assert payload["email"] == "smoke@example.com"


def test_valid_session_checkout_dev_without_stripe(client):
    c, _mod = client
    tok = _verified_token(c, "trial@example.com")
    res = c.post(
        "/v1/checkout",
        headers={"Authorization": f"Bearer {tok}"},
        json={"trial": True, "quantity": 1},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("mode") == "dev"
    assert "trial" in (body.get("message") or "").lower()
    assert body.get("entitlement", {}).get("share_anonymized_outcomes") is True


def test_signed_session_requires_active_server_row(client):
    """cs2 tokens are revoked when the server session row is deleted."""
    c, _mod = client
    tok = _verified_token(c, "signed@example.com")
    assert tok.startswith("cs2.")
    assert c.get("/v1/entitlement", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.post("/v1/logout", headers={"Authorization": f"Bearer {tok}"}).status_code == 200
    assert c.get("/v1/entitlement", headers={"Authorization": f"Bearer {tok}"}).status_code == 401


def test_env_wins_over_disk_in_production(tmp_path, monkeypatch):
    import main as mod

    monkeypatch.setattr(mod, "DATA", tmp_path)
    monkeypatch.setattr(mod, "STORE", tmp_path / "accounts.json")
    secret_path = tmp_path / "session_secret"
    monkeypatch.setattr(mod, "_SESSION_SECRET_PATH", secret_path)
    secret_path.write_text("old-disk-pin", encoding="utf-8")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", "shared-env-group-secret")
    assert mod._resolve_session_secret() == "shared-env-group-secret"


def test_empty_session_secret_in_production_refuses_to_boot(tmp_path, monkeypatch):
    import main as mod

    monkeypatch.setattr(mod, "DATA", tmp_path)
    monkeypatch.setattr(mod, "_SESSION_SECRET_PATH", tmp_path / "session_secret")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("SESSION_SECRET", raising=False)
    with pytest.raises(RuntimeError, match="SESSION_SECRET"):
        mod._resolve_session_secret()


def test_checkout_disables_managed_payments(client, monkeypatch):
    c, mod = client
    monkeypatch.setattr(mod, "STRIPE_SECRET", "sk_test_dummy")
    monkeypatch.setattr(mod, "STRIPE_PRICE_ID", "price_test_dummy")
    tok = _verified_token(c, "pay@example.com")

    captured: dict = {}

    class _FakeSession:
        url = "https://checkout.stripe.com/c/pay/cs_test_fake"

    class _FakeCheckoutSession:
        @staticmethod
        def create(**kwargs):
            captured.update(kwargs)
            return _FakeSession()

    class _FakeCheckout:
        Session = _FakeCheckoutSession

    class _FakeStripe:
        api_key = None
        checkout = _FakeCheckout()

    monkeypatch.setitem(__import__("sys").modules, "stripe", _FakeStripe)

    res = c.post(
        "/v1/checkout",
        headers={"Authorization": f"Bearer {tok}"},
        json={"trial": True, "quantity": 1},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("mode") == "stripe"
    assert body.get("url") == _FakeSession.url
    assert captured.get("managed_payments") == {"enabled": False}
    assert captured.get("mode") == "subscription"
