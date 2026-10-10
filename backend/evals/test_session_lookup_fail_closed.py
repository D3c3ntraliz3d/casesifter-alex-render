"""P0: cp_sessions lookup errors → 503 in production (never fail open)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import time

import pytest
from fastapi.testclient import TestClient


def _cs2(secret: str, *, user_id: str, email: str, sid: str) -> str:
    issued = int(time.time())
    payload = f"{user_id}|{email}|{sid}|{issued}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"cs2.{b64(payload)}.{b64(sig)}"


def test_session_lookup_error_returns_503_in_production(tmp_path, monkeypatch):
    secret = "unit-test-session-lookup-secret"
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("DATABASE_URL", "postgresql://invalid:invalid@127.0.0.1:1/none")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "ops-key-lookup")
    monkeypatch.setenv("CLOUD_CONTROL_URL", "https://control.test.local")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-session-lookup")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("AUTH_SQLITE_PATH", raising=False)
    (tmp_path / "data").mkdir()

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod
    import app.session_auth as session_auth

    reset_store()
    session_auth.reset_cp_engine_for_tests()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)

    monkeypatch.setattr(
        "app.v1_analysis._probe_control_plane_session",
        lambda _t: {"email": "a@example.com", "plan": "trial", "active": True},
    )

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    client = TestClient(main_mod.app)
    tok = _cs2(secret, user_id="a" * 32, email="a@example.com", sid="s" + "b" * 31)
    res = client.get("/api/v1/matters", headers={"Authorization": f"Bearer {tok}"})
    assert res.status_code == 503
    assert "unavailable" in res.json()["detail"].lower()
    session_auth.reset_cp_engine_for_tests()


def test_cs1_rejected_after_max_age(monkeypatch):
    from app.session_auth import parse_session_token

    secret = "cs1-cutoff-secret"
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("CS1_MAX_AGE_DAYS", "7")
    monkeypatch.delenv("CS1_ACCEPT_UNTIL", raising=False)

    issued = int(time.time()) - (8 * 86400)
    payload = f"old@example.com|{issued}|deadbeef".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()
    b64p = base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")
    b64s = base64.urlsafe_b64encode(sig).decode("ascii").rstrip("=")
    token = f"cs1.{b64p}.{b64s}"
    assert parse_session_token(token) is None
