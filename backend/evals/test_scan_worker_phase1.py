"""Scale Phase 1: worker round-trip, auth, idempotency, retry, bundle delete."""

from __future__ import annotations

import io
import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-phase1-worker")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKER_SERVICE_TOKEN", "test-worker-secret")
    monkeypatch.setenv("SCAN_BACKEND", "worker")
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("QUEUE_BACKEND", "local")
    monkeypatch.setenv("APP_MODE", "vault")
    monkeypatch.setenv("APP_ENV", "development")
    (tmp_path / "data").mkdir()

    from app.config import Settings
    import app.config as config_mod
    import app.blob_store as blob_mod
    import app.job_queue as queue_mod
    import app.storage as storage_mod

    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(storage_mod, "settings", settings)
    monkeypatch.setattr(blob_mod, "settings", settings)
    monkeypatch.setattr(queue_mod, "settings", settings)
    blob_mod.reset_blob_store_for_tests(None)
    queue_mod.reset_job_queue_for_tests(None)

    from app.blob_store import LocalBlobStore, get_blob_store
    from app.job_queue import LocalJobQueue, get_job_queue

    store = LocalBlobStore(settings.blob_dir)
    queue = LocalJobQueue(settings.queue_dir)
    blob_mod.reset_blob_store_for_tests(store)
    queue_mod.reset_job_queue_for_tests(queue)

    return {
        "settings": settings,
        "store": store,
        "queue": queue,
        "tmp": tmp_path,
        "token": "test-worker-secret",
    }


def _tiny_pdf(path: Path) -> None:
    # Minimal valid-enough PDF bytes for storage tests (pipeline mocked).
    path.write_bytes(b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n")


def test_result_endpoint_rejects_bad_token(worker_env, monkeypatch):
    from app.main import app

    client = TestClient(app)
    res = client.post(
        "/api/internal/v1/jobs/" + ("a" * 32) + "/result",
        json={"status": "failed", "error": "x"},
        headers={"Authorization": "Bearer wrong"},
    )
    assert res.status_code == 401


def test_worker_round_trip_deletes_bundle(worker_env, monkeypatch):
    from app.blob_store import job_meta_key, job_source_key
    from app.job_queue import QueueMessage
    from app.models import AuditTrail, Report
    from app.storage import load_progress, load_report
    from worker.run_job import process_queue_message
    from worker import client as worker_client

    env = worker_env
    job_id = "b" * 32
    source_name = "exhibit.pdf"
    src = env["tmp"] / source_name
    _tiny_pdf(src)

    meta = {
        "job_id": job_id,
        "firm_id": "firm-1",
        "matter_id": None,
        "persist": False,
        "source_name": source_name,
        "prior_snapshots": [],
        "submitted_by": "test",
        "attempt": 1,
        "max_attempts": 3,
    }
    env["store"].put(job_source_key(job_id, source_name), src.read_bytes())
    env["store"].put(job_meta_key(job_id), json.dumps(meta).encode("utf-8"))

    from datetime import datetime, timezone

    fake_report = Report(
        job_id=job_id,
        risk_level="green",
        risk_score=10,
        ai_review="complete",
        executive_summary="ok",
        top_findings=[],
        recommended_actions=[],
        findings=[],
        timeline=[],
        page_dimensions=[],
        fonts=[],
        comparison={"facts": [], "ai_review": "complete"},
        modules=[],
        audit=AuditTrail(
            scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            analysis_version="test",
            modules_run=[],
            submitted_by="test",
            filename=source_name,
            file_size_bytes=src.stat().st_size,
            file_type="application/pdf",
            sha256="c" * 64,
        ),
        artifact_urls=[],
    )

    monkeypatch.setattr("worker.run_job.run_local_stage", lambda *a, **k: None)
    monkeypatch.setattr("worker.run_job.run_gemini_stage", lambda *a, **k: fake_report)

    # Callback into the real FastAPI app in-process.
    from app.main import app

    client = TestClient(app)

    posts: list[str] = []

    def fake_post(job_id_arg, path, payload):
        posts.append(path)
        headers = {"Authorization": f"Bearer {env['token']}"}
        res = client.post(path if path.startswith("/api") else f"/api{path}", json=payload, headers=headers)
        assert res.status_code < 400, res.text
        return res.json()

    monkeypatch.setattr(worker_client, "_post", fake_post)

    msg = QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=1)
    process_queue_message(msg)

    assert any(p.endswith("/result") for p in posts), posts
    progress = load_progress(job_id)
    assert progress is not None
    assert progress.status == "complete"
    # progress.json is stored without the report blob; report lives in report.json.
    report = load_report(job_id)
    assert report is not None
    assert report.ai_review == "complete"
    # Bundle deleted after accepted result.
    assert not env["store"].exists(job_meta_key(job_id))
    assert not env["store"].exists(job_source_key(job_id, source_name))


def test_duplicate_result_is_idempotent(worker_env):
    from app.main import app
    from app.models import AuditTrail, Report
    from app.storage import create_job, update_job

    job_id = "d" * 32
    create_job(job_id, "x.pdf")
    from datetime import datetime, timezone

    report = Report(
        job_id=job_id,
        risk_level="green",
        risk_score=1,
        executive_summary="ok",
        top_findings=[],
        recommended_actions=[],
        findings=[],
        timeline=[],
        page_dimensions=[],
        fonts=[],
        comparison={},
        modules=[],
        audit=AuditTrail(
            scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc),
            analysis_version="t",
            modules_run=[],
            submitted_by="t",
            filename="x.pdf",
            file_size_bytes=1,
            file_type="application/pdf",
            sha256="e" * 64,
        ),
        artifact_urls=[],
    )
    update_job(job_id, status="complete", stage="complete", percent=100, message="done", report=report)

    client = TestClient(app)
    res1 = client.post(
        f"/api/internal/v1/jobs/{job_id}/result",
        json={"status": "complete", "report": report.model_dump(mode="json"), "message": "again"},
        headers={"Authorization": "Bearer test-worker-secret"},
    )
    assert res1.status_code == 200
    assert res1.json().get("idempotent") is True


def test_bundle_deleted_after_failure_result(worker_env):
    from app.blob_store import job_meta_key, job_source_key
    from app.main import app

    job_id = "f" * 32
    env = worker_env
    env["store"].put(job_meta_key(job_id), b'{"job_id":"%s"}' % job_id.encode())
    env["store"].put(job_source_key(job_id, "a.pdf"), b"%PDF")

    client = TestClient(app)
    res = client.post(
        f"/api/internal/v1/jobs/{job_id}/result",
        json={"status": "failed", "error": "boom", "message": "Analysis failed."},
        headers={"Authorization": "Bearer test-worker-secret"},
    )
    assert res.status_code == 200
    assert not env["store"].exists(job_meta_key(job_id))


def test_crash_retries_then_dead_letters(worker_env, monkeypatch):
    from app.job_queue import QueueMessage, get_job_queue, run_local_worker_loop
    from worker.run_job import WorkerJobError

    q = get_job_queue()
    job_id = "1" * 32
    q.enqueue(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=1))

    calls = {"n": 0}

    def boom(msg):
        calls["n"] += 1
        raise WorkerJobError("simulated crash")

    stop = threading.Event()

    def _run():
        run_local_worker_loop(boom, idle_sleep_s=0.05, stop_event=stop)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    deadline = time.time() + 5
    while time.time() < deadline and calls["n"] < 3:
        time.sleep(0.05)
    stop.set()
    t.join(timeout=2)
    assert calls["n"] >= 3
    dead = list((worker_env["settings"].queue_dir / "dead").glob("*.json"))
    assert any(job_id in p.name for p in dead)


def test_dispatch_writes_bundle_and_enqueues(worker_env, monkeypatch):
    from app.blob_store import job_meta_key, job_source_key
    from app.scan_dispatch import dispatch_ephemeral_job

    job_id = "2" * 32
    dest = worker_env["tmp"] / "upload.pdf"
    _tiny_pdf(dest)
    from app.storage import create_job

    create_job(job_id, dest.name)
    # Place file where payload expects it.
    job_dest = worker_env["settings"].jobs_dir / job_id / dest.name
    job_dest.parent.mkdir(parents=True, exist_ok=True)
    job_dest.write_bytes(dest.read_bytes())

    backend = dispatch_ephemeral_job(
        {
            "job_id": job_id,
            "dest": str(job_dest),
            "submitted_by": "test",
            "prior_snapshots": [],
            "firm_ctx": {"firm_id": "firm-x", "user_id": "u1"},
            "matter_id": "matter-1",
            "persist": False,
        }
    )
    assert backend == "worker"
    assert worker_env["store"].exists(job_meta_key(job_id))
    assert worker_env["store"].exists(job_source_key(job_id, dest.name))
    assert worker_env["queue"].ready_count() == 1
    # Web copy of source removed after blob put.
    assert not job_dest.exists()
