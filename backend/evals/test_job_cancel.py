"""Cancel pending/running analysis jobs (firm-scoped, sticky, idempotent)."""

from __future__ import annotations

import base64
import hashlib
import hmac
import io
import time
from pathlib import Path

from fastapi.testclient import TestClient

_SECRET = "unit-test-session-secret-job-cancel"


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
    monkeypatch.setenv("ANALYSIS_API_KEYS", "ops-key-cancel")
    monkeypatch.setenv("CORS_ALLOW_ORIGINS", "https://casesifter.com")
    monkeypatch.setenv("CLOUD_CONTROL_URL", "https://control.test.local")
    monkeypatch.setenv("PERSIST_FACTS", "true")
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-job-cancel")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.setenv("SCAN_BACKEND", "worker")
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("QUEUE_BACKEND", "local")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    data_dir = tmp_path / "data"
    data_dir.mkdir()
    monkeypatch.setenv("DATA_DIR", str(data_dir))

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod
    import app.job_queue as queue_mod
    import app.blob_store as blob_mod

    reset_store()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    monkeypatch.setattr("app.v1_analysis.settings", settings)
    monkeypatch.setattr("app.ephemeral.settings", settings)
    monkeypatch.setattr("app.storage.settings", settings)
    monkeypatch.setattr("app.job_queue.settings", settings)
    monkeypatch.setattr("app.blob_store.settings", settings)
    monkeypatch.setattr("app.scan_dispatch.settings", settings)

    from app.job_queue import LocalJobQueue

    queue = LocalJobQueue(settings.queue_dir)
    queue_mod.reset_job_queue_for_tests(queue)
    blob_mod.reset_blob_store_for_tests(None)

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
    return TestClient(main_mod.app), settings, tok_a, tok_b, queue


def _submit(client, headers, matter_id: str) -> str:
    pdf = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"
    job = client.post(
        "/api/v1/jobs",
        headers=headers,
        files={"file": ("dummy.pdf", io.BytesIO(pdf), "application/pdf")},
        data={"matter_id": matter_id, "submitted_by": "test"},
    )
    assert job.status_code == 200, job.text
    return job.json()["job_id"]


def test_cancel_queued_job_drops_ready_and_is_idempotent(tmp_path, monkeypatch):
    client, settings, tok_a, tok_b, queue = _build_client(tmp_path, monkeypatch)
    ha = {"Authorization": f"Bearer {tok_a}"}
    hb = {"Authorization": f"Bearer {tok_b}"}

    matter = client.post("/api/v1/matters", headers=ha, json={"name": "cancel-a"}).json()
    mid = matter["id"]
    jid = _submit(client, ha, mid)
    assert queue.ready_count() == 1

    # Cross-firm cancel looks like unknown.
    assert client.post(f"/api/v1/jobs/{jid}/cancel", headers=hb).status_code == 404

    res = client.post(f"/api/v1/jobs/{jid}/cancel", headers=ha)
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["status"] == "cancelled"
    assert body["cancelled"] is True
    assert queue.ready_count() == 0

    status = client.get(f"/api/v1/jobs/{jid}", headers=ha)
    assert status.status_code == 200
    assert status.json()["status"] == "cancelled"

    again = client.post(f"/api/v1/jobs/{jid}/cancel", headers=ha)
    assert again.status_code == 200
    assert again.json()["already_terminal"] is True
    assert again.json()["status"] == "cancelled"

    # Matter is not stuck — same file can be re-queued.
    jid2 = _submit(client, ha, mid)
    assert jid2 != jid
    assert queue.ready_count() == 1


def test_cancel_running_sets_flag_and_sticky(tmp_path, monkeypatch):
    client, _settings, tok_a, _tok_b, queue = _build_client(tmp_path, monkeypatch)
    ha = {"Authorization": f"Bearer {tok_a}"}
    matter = client.post("/api/v1/matters", headers=ha, json={"name": "cancel-run"}).json()
    jid = _submit(client, ha, matter["id"])

    from app.fact_memory.store import get_store
    from app.storage import update_job
    from app.gemini_cancel import clear_cancel, is_cancelled

    clear_cancel(jid)
    get_store().update_analysis_job(jid, status="running", message="Scanning…")
    update_job(jid, status="running", stage="contextual", percent=50, message="Scanning…")
    # Claim so drop_ready cannot remove it (simulates in-flight).
    msg = queue.claim()
    assert msg is not None and msg.job_id == jid

    res = client.post(f"/api/v1/jobs/{jid}/cancel", headers=ha)
    assert res.status_code == 200, res.text
    assert res.json()["status"] == "cancelled"
    assert is_cancelled(jid)

    from app.job_cancel import worker_cancel_requested

    assert worker_cancel_requested(jid)

    progress = client.get(f"/api/v1/jobs/{jid}", headers=ha).json()
    assert progress["status"] == "cancelled"
    assert progress["error"] == "cancelled_by_user"


def test_matter_cancel_jobs_batch(tmp_path, monkeypatch):
    client, _settings, tok_a, _tok_b, queue = _build_client(tmp_path, monkeypatch)
    ha = {"Authorization": f"Bearer {tok_a}"}
    matter = client.post("/api/v1/matters", headers=ha, json={"name": "cancel-batch"}).json()
    mid = matter["id"]
    # Distinct PDFs so server dedupe does not collapse them.
    jids = []
    for i in range(2):
        pdf = f"%PDF-1.4\n%% cancel-batch-{i}\ntrailer<<>>\n%%EOF\n".encode("ascii")
        job = client.post(
            "/api/v1/jobs",
            headers=ha,
            files={"file": (f"b{i}.pdf", io.BytesIO(pdf), "application/pdf")},
            data={"matter_id": mid, "submitted_by": "test"},
        )
        assert job.status_code == 200, job.text
        jids.append(job.json()["job_id"])
    assert queue.ready_count() == 2

    res = client.post(f"/api/v1/matters/{mid}/cancel-jobs", headers=ha, json={})
    assert res.status_code == 200, res.text
    body = res.json()
    assert body["cancelled"] == 2
    assert queue.ready_count() == 0


def test_cancelled_overlay_on_document_row():
    from app.scan_row import overlay_inflight_scans

    docs = [
        {
            "id": "d1",
            "sha256": "abc",
            "status": "complete",
            "risk_level": "red",
            "scan_job_id": "old",
        }
    ]
    out = overlay_inflight_scans(
        docs,
        {
            "abc": {
                "job_id": "new",
                "status": "cancelled",
                "error": "cancelled_by_user",
                "message": "Scan cancelled.",
            }
        },
    )[0]
    assert out["status"] == "cancelled"
    assert out["scan_in_progress"] is False
    assert out["scan_failed_open"] is True
    assert out["risk_level"] is None
    assert out["last_error"] == "cancelled_by_user"
