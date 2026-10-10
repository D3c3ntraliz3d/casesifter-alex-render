"""P0-2: job endpoints are firm-scoped; job_id path traversal is rejected."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import time
from pathlib import Path

from fastapi.testclient import TestClient

_SECRET = "unit-test-session-secret-job-owner"


def _cs2(*, user_id: str, email: str, sid: str) -> str:
    issued = int(time.time())
    payload = f"{user_id}|{email}|{sid}|{issued}".encode("utf-8")
    sig = hmac.new(_SECRET.encode("utf-8"), payload, hashlib.sha256).digest()

    def b64(raw: bytes) -> str:
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    return f"cs2.{b64(payload)}.{b64(sig)}"


def _build_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SESSION_SECRET", _SECRET)
    monkeypatch.setenv("ANALYSIS_API_KEYS", "ops-key-isolation")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    monkeypatch.setenv("CLOUD_CONTROL_URL", "https://control.test.local")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-job-owner")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.ephemeral.settings", settings)
    monkeypatch.setattr("app.storage.settings", settings)

    tok_a = _cs2(user_id="a" * 32, email="firm-a@example.com", sid="s" + "a" * 31)
    tok_b = _cs2(user_id="b" * 32, email="firm-b@example.com", sid="s" + "b" * 31)

    def _probe(bearer: str):
        if bearer == tok_a:
            return {"email": "firm-a@example.com", "plan": "trial", "active": True, "user_id": "a" * 32}
        if bearer == tok_b:
            return {"email": "firm-b@example.com", "plan": "trial", "active": True, "user_id": "b" * 32}
        return None

    monkeypatch.setattr("app.v1_analysis._probe_control_plane_session", _probe)
    monkeypatch.setattr("app.session_auth.session_row_active", lambda _sid: None)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.app), settings, tok_a, tok_b


def test_cross_firm_job_status_report_delete_are_404(tmp_path, monkeypatch):
    client, settings, tok_a, tok_b = _build_client(tmp_path, monkeypatch)
    ha = {"Authorization": f"Bearer {tok_a}"}
    hb = {"Authorization": f"Bearer {tok_b}"}

    matter = client.post("/api/v1/matters", headers=ha, json={"name": "iso-a"}).json()
    mid = matter["id"]
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    job = client.post(
        "/api/v1/jobs",
        headers=ha,
        files={"file": ("dummy.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"matter_id": mid, "submitted_by": "test"},
    )
    assert job.status_code == 200, job.text
    jid = job.json()["job_id"]
    assert len(jid) == 32

    # Firm A can see own job.
    assert client.get(f"/api/v1/jobs/{jid}", headers=ha).status_code == 200

    # Firm B cannot status/report/delete A's job.
    assert client.get(f"/api/v1/jobs/{jid}", headers=hb).status_code == 404
    assert client.get(f"/api/v1/jobs/{jid}/report", headers=hb).status_code == 404
    assert client.delete(f"/api/v1/jobs/{jid}", headers=hb).status_code == 404

    # A's job still present after B's delete attempt.
    assert client.get(f"/api/v1/jobs/{jid}", headers=ha).status_code == 200


def test_path_traversal_delete_rejected_and_canary_survives(tmp_path, monkeypatch):
    client, settings, _tok_a, tok_b = _build_client(tmp_path, monkeypatch)
    hb = {"Authorization": f"Bearer {tok_b}"}
    canary = Path(settings.data_dir) / "canary.txt"
    canary.write_text("keep-me", encoding="utf-8")

    res = client.request(
        "DELETE",
        "/api/v1/jobs/%2e%2e",
        headers=hb,
    )
    assert res.status_code == 404, res.text
    assert canary.is_file()
    assert canary.read_text(encoding="utf-8") == "keep-me"

    # Bare ".." also rejected.
    assert client.delete("/api/v1/jobs/..", headers=hb).status_code == 404
    assert canary.is_file()


def test_sweep_requires_api_key(tmp_path, monkeypatch):
    client, _settings, tok_a, _tok_b = _build_client(tmp_path, monkeypatch)
    session = client.post("/api/v1/sweep", headers={"Authorization": f"Bearer {tok_a}"})
    assert session.status_code == 403
    ops = client.post("/api/v1/sweep", headers={"X-API-Key": "ops-key-isolation"})
    assert ops.status_code == 200
