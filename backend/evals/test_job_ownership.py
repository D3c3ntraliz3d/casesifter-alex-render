"""P0-2: job endpoints are firm-scoped; job_id path traversal is rejected."""

from __future__ import annotations

import io
from pathlib import Path

from fastapi.testclient import TestClient


def _build_client(tmp_path, monkeypatch):
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("APP_ENV", "development")
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

    def _probe(bearer: str):
        if bearer == "tok-firm-a":
            return {"email": "firm-a@example.com", "plan": "trial", "active": True}
        if bearer == "tok-firm-b":
            return {"email": "firm-b@example.com", "plan": "trial", "active": True}
        return None

    monkeypatch.setattr("app.v1_analysis._probe_control_plane_session", _probe)

    import importlib
    import app.main as main_mod

    importlib.reload(main_mod)
    return TestClient(main_mod.app), settings


def test_cross_firm_job_status_report_delete_are_404(tmp_path, monkeypatch):
    client, settings = _build_client(tmp_path, monkeypatch)
    ha = {"Authorization": "Bearer tok-firm-a"}
    hb = {"Authorization": "Bearer tok-firm-b"}

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
    client, settings = _build_client(tmp_path, monkeypatch)
    hb = {"Authorization": "Bearer tok-firm-b"}
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
    client, _settings = _build_client(tmp_path, monkeypatch)
    session = client.post("/api/v1/sweep", headers={"Authorization": "Bearer tok-firm-a"})
    assert session.status_code == 403
    ops = client.post("/api/v1/sweep", headers={"X-API-Key": "ops-key-isolation"})
    assert ops.status_code == 200
