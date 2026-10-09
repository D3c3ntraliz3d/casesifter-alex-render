"""Smoke: invalid Bearer → 401 Invalid session; entitlement always shares outcomes."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import main as mod  # noqa: E402

client = TestClient(mod.app)


def test_checkout_rejects_bad_session():
    res = client.post(
        "/v1/checkout",
        headers={"Authorization": "Bearer not-a-real-session"},
        json={"trial": True, "quantity": 1},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid session."


def test_entitlement_rejects_bad_session():
    res = client.get(
        "/v1/entitlement",
        headers={"Authorization": "Bearer not-a-real-session"},
    )
    assert res.status_code == 401
    assert res.json()["detail"] == "Invalid session."


def test_entitlement_payload_share_always_on():
    user = {
        "email": "smoke@example.com",
        "subscription": {},
        "trial_until": 0,
    }
    payload = mod._entitlement_payload(user)
    assert payload["share_anonymized_outcomes"] is True
    assert payload["email"] == "smoke@example.com"


def test_valid_session_checkout_dev_without_stripe(tmp_path, monkeypatch):
    store = tmp_path / "accounts.json"
    monkeypatch.setattr(mod, "STORE", store)
    monkeypatch.setattr(mod, "DATA", tmp_path)
    monkeypatch.setattr(mod, "STRIPE_SECRET", "")
    monkeypatch.setattr(mod, "STRIPE_PRICE_ID", "")
    data = {
        "users": {},
        "sessions": {"tok_ok": {"email": "trial@example.com", "created_at": 1}},
        "trials": {},
        "trial_domains": {},
        "trial_emails": {},
    }
    data["users"]["trial@example.com"] = {
        "email": "trial@example.com",
        "name": "",
        "salt": "x",
        "password_hash": "y",
        "created_at": 1,
        "subscription": {},
        "trial_until": 0,
    }
    mod._ensure_user_identity(data["users"]["trial@example.com"])
    store.write_text(__import__("json").dumps(data), encoding="utf-8")

    res = client.post(
        "/v1/checkout",
        headers={"Authorization": "Bearer tok_ok"},
        json={"trial": True, "quantity": 1},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("mode") == "dev"
    assert "trial" in (body.get("message") or "").lower()
    assert body.get("entitlement", {}).get("share_anonymized_outcomes") is True


def test_signed_session_survives_sessions_wipe(tmp_path, monkeypatch):
    """SESSION_SECRET tokens remain valid even if accounts.json sessions are cleared."""
    store = tmp_path / "accounts.json"
    monkeypatch.setattr(mod, "STORE", store)
    monkeypatch.setattr(mod, "DATA", tmp_path)
    monkeypatch.setattr(mod, "SESSION_SECRET", "unit-test-session-secret")
    monkeypatch.setattr(mod, "STRIPE_SECRET", "")
    monkeypatch.setattr(mod, "STRIPE_PRICE_ID", "")

    data = {
        "users": {},
        "sessions": {},
        "trials": {},
        "trial_domains": {},
        "trial_emails": {},
    }
    data["users"]["signed@example.com"] = {
        "email": "signed@example.com",
        "name": "",
        "salt": "x",
        "password_hash": "y",
        "created_at": 1,
        "subscription": {},
        "trial_until": 0,
    }
    mod._ensure_user_identity(data["users"]["signed@example.com"])
    token = mod._issue_session_token("signed@example.com", data)
    assert token.startswith("cs1.")
    # Simulate deploy wipe of opaque sessions only.
    data["sessions"] = {}
    store.write_text(__import__("json").dumps(data), encoding="utf-8")

    res = client.get(
        "/v1/entitlement",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert res.status_code == 200, res.text
    assert res.json().get("email") == "signed@example.com"


def test_checkout_disables_managed_payments(tmp_path, monkeypatch):
    store = tmp_path / "accounts.json"
    monkeypatch.setattr(mod, "STORE", store)
    monkeypatch.setattr(mod, "DATA", tmp_path)
    monkeypatch.setattr(mod, "STRIPE_SECRET", "sk_test_dummy")
    monkeypatch.setattr(mod, "STRIPE_PRICE_ID", "price_test_dummy")

    data = {
        "users": {},
        "sessions": {"tok_ok": {"email": "pay@example.com", "created_at": 1}},
        "trials": {},
        "trial_domains": {},
        "trial_emails": {},
    }
    data["users"]["pay@example.com"] = {
        "email": "pay@example.com",
        "name": "",
        "salt": "x",
        "password_hash": "y",
        "created_at": 1,
        "subscription": {},
        "trial_until": 0,
    }
    mod._ensure_user_identity(data["users"]["pay@example.com"])
    store.write_text(__import__("json").dumps(data), encoding="utf-8")

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

    res = client.post(
        "/v1/checkout",
        headers={"Authorization": "Bearer tok_ok"},
        json={"trial": True, "quantity": 1},
    )
    assert res.status_code == 200, res.text
    body = res.json()
    assert body.get("mode") == "stripe"
    assert body.get("url") == _FakeSession.url
    assert captured.get("managed_payments") == {"enabled": False}
    assert captured.get("mode") == "subscription"
