"""P0-5: authenticated vs entitled; CP down → 503; expired trial reads OK."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import time

from fastapi.testclient import TestClient


def _cs2_token(*, secret: str, user_id: str, email: str, sid: str) -> str:
    issued = int(time.time())
    payload = f"{user_id}|{email}|{sid}|{issued}".encode("utf-8")
    sig = hmac.new(secret.encode("utf-8"), payload, hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"cs2.{b64(payload)}.{b64(sig)}"


def _client(tmp_path, monkeypatch, *, entitle_active: bool, cp_down: bool = False):
    secret = "analysis-unit-session-secret"
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SESSION_SECRET", secret)
    monkeypatch.setenv("ANALYSIS_API_KEYS", "ops-key-p05")
    monkeypatch.setenv("CLOUD_CONTROL_URL", "https://control.test.local")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-p05-auth")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "data").mkdir()

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.storage.settings", settings)

    def _probe(token: str):
        from app.v1_analysis import ControlPlaneUnavailable

        if cp_down:
            raise ControlPlaneUnavailable("down")
        return {
            "active": entitle_active,
            "email": "reader@example.com",
            "plan": "trial" if entitle_active else None,
            "user_id": "u" + "a" * 31,
        }

    monkeypatch.setattr("app.v1_analysis._probe_control_plane_session", _probe)
    # No cp_sessions table in unit test — HMAC-only accept.
    monkeypatch.setattr("app.session_auth.session_row_active", lambda _sid: None)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.app), secret


def test_expired_trial_can_read_matters_but_not_post_jobs(tmp_path, monkeypatch):
    client, secret = _client(tmp_path, monkeypatch, entitle_active=False)
    token = _cs2_token(
        secret=secret,
        user_id="u" + "a" * 31,
        email="reader@example.com",
        sid="s" + "b" * 31,
    )
    h = {"Authorization": f"Bearer {token}"}
    assert client.get("/api/v1/matters", headers=h).status_code == 200
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    job = client.post(
        "/api/v1/jobs",
        headers=h,
        files={"file": ("dummy.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"submitted_by": "test"},
    )
    assert job.status_code == 402


def test_control_plane_down_returns_503_not_401_on_write(tmp_path, monkeypatch):
    client, secret = _client(tmp_path, monkeypatch, entitle_active=True, cp_down=True)
    token = _cs2_token(
        secret=secret,
        user_id="u" + "c" * 31,
        email="reader@example.com",
        sid="s" + "d" * 31,
    )
    h = {"Authorization": f"Bearer {token}"}
    # Reads still authenticate locally.
    assert client.get("/api/v1/matters", headers=h).status_code == 200
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    job = client.post(
        "/api/v1/jobs",
        headers=h,
        files={"file": ("dummy.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"submitted_by": "test"},
    )
    assert job.status_code == 503
    assert "unavailable" in job.json()["detail"].lower()
