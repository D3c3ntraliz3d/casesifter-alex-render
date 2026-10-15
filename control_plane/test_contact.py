"""Marketing contact form → Resend email to sales@."""

from __future__ import annotations

import io
import json
import sys
from pathlib import Path
from urllib.error import HTTPError

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


@pytest.fixture()
def client(tmp_path, monkeypatch):
    monkeypatch.setenv("CONTROL_DATA_DIR", str(tmp_path))
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("SESSION_SECRET", "unit-test-contact-secret")
    monkeypatch.setenv("RESEND_API_KEY", "re_test_key")
    monkeypatch.setenv("CONTACT_TO_EMAIL", "sales@casesifter.com")
    monkeypatch.setenv("CONTACT_FROM_EMAIL", "CaseSifter <noreply@casesifter.com>")

    import auth_store
    import importlib
    import main as mod

    auth_store.reset_auth_store_for_tests()
    importlib.reload(mod)
    mod.SESSION_SECRET = "unit-test-contact-secret"
    mod.RESEND_API_KEY = "re_test_key"
    mod.CONTACT_TO_EMAIL = "sales@casesifter.com"
    mod.CONTACT_FROM_EMAIL = "CaseSifter <noreply@casesifter.com>"
    yield TestClient(mod.app), mod
    auth_store.reset_auth_store_for_tests()


def test_contact_sends_via_resend(client, monkeypatch):
    c, mod = client
    captured: dict = {}

    class _Resp:
        status = 200

        def __enter__(self):
            return self

        def __exit__(self, *args):
            return False

        def read(self):
            return b'{"id":"email_test"}'

    def fake_urlopen(req, timeout=20):
        captured["url"] = req.full_url
        captured["headers"] = dict(req.headers)
        captured["body"] = json.loads(req.data.decode("utf-8"))
        return _Resp()

    monkeypatch.setattr(mod.urllib.request, "urlopen", fake_urlopen)

    res = c.post(
        "/v1/contact",
        json={
            "name": "Ada",
            "firm": "Ada LLP",
            "email": "ada@firm.com",
            "phone": "555-0100",
            "reason": "demo",
            "message": "We handle foreclosure matters.",
        },
    )
    assert res.status_code == 200, res.text
    assert res.json().get("ok") is True
    assert captured["url"] == "https://api.resend.com/emails"
    assert captured["body"]["to"] == ["sales@casesifter.com"]
    assert captured["body"]["reply_to"] == "ada@firm.com"
    assert "Ada LLP" in captured["body"]["text"]
    assert "Authorization" in captured["headers"] or "authorization" in {
        k.lower(): v for k, v in captured["headers"].items()
    }


def test_contact_requires_resend_key(client, monkeypatch):
    c, mod = client
    monkeypatch.setattr(mod, "RESEND_API_KEY", "")
    res = c.post(
        "/v1/contact",
        json={
            "name": "Ada",
            "firm": "Ada LLP",
            "email": "ada@firm.com",
            "reason": "demo",
            "message": "hi",
        },
    )
    assert res.status_code == 503


def test_contact_surfaces_resend_failure(client, monkeypatch):
    c, mod = client

    def boom(req, timeout=20):
        raise HTTPError(req.full_url, 401, "Unauthorized", hdrs=None, fp=io.BytesIO(b'{"message":"bad"}'))

    monkeypatch.setattr(mod.urllib.request, "urlopen", boom)
    res = c.post(
        "/v1/contact",
        json={
            "name": "Ada",
            "firm": "Ada LLP",
            "email": "ada@firm.com",
            "reason": "demo",
            "message": "hi",
        },
    )
    assert res.status_code == 502
