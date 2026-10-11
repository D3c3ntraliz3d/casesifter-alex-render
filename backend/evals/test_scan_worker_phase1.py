"""Scale Phase 1: worker round-trip, auth, idempotency, retry, bundle delete."""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient


@pytest.fixture()
def worker_env(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-phase1-worker")
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(tmp_path / "fm.sqlite3"))
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("WORKER_SERVICE_TOKEN", "test-worker-secret")
    monkeypatch.setenv("SCAN_BACKEND", "worker")
    monkeypatch.setenv("BLOB_BACKEND", "local")
    monkeypatch.setenv("QUEUE_BACKEND", "local")
    monkeypatch.setenv("APP_MODE", "vault")
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("SESSION_SECRET", "unit-test-session-secret-phase1")
    monkeypatch.delenv("DATABASE_URL", raising=False)
    (tmp_path / "data").mkdir()

    from app.fact_memory.store import reset_store
    from app.config import Settings
    import app.config as config_mod
    import app.blob_store as blob_mod
    import app.job_queue as queue_mod
    import app.storage as storage_mod
    import app.worker_auth as worker_auth_mod
    import app.internal_jobs as internal_mod
    import app.scan_dispatch as dispatch_mod
    import app.ephemeral as ephemeral_mod
    import worker.client as worker_client_mod
    import worker.run_job as run_job_mod

    reset_store()
    settings = Settings()
    # Patch every module that bound ``settings`` at import time (avoids 503 from
    # worker_auth reading the process-global Settings without WORKER_SERVICE_TOKEN).
    for mod in (
        config_mod,
        storage_mod,
        blob_mod,
        queue_mod,
        worker_auth_mod,
        internal_mod,
        dispatch_mod,
        ephemeral_mod,
        worker_client_mod,
        run_job_mod,
    ):
        monkeypatch.setattr(mod, "settings", settings)

    try:
        import app.main as main_mod

        monkeypatch.setattr(main_mod, "settings", settings)
    except Exception:
        pass
    try:
        import app.v1_analysis as v1_mod

        monkeypatch.setattr(v1_mod, "settings", settings)
    except Exception:
        pass

    blob_mod.reset_blob_store_for_tests(None)
    queue_mod.reset_job_queue_for_tests(None)

    from app.blob_store import LocalBlobStore
    from app.job_queue import LocalJobQueue

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


def _hmac_headers(job_id: str, body: bytes, token: str = "test-worker-secret") -> dict[str, str]:
    import hashlib
    import hmac as hm

    ts = str(int(time.time()))
    body_hash = hashlib.sha256(body or b"").hexdigest()
    msg = f"{job_id}:{ts}:{body_hash}".encode("utf-8")
    sig = hm.new(token.encode("utf-8"), msg, hashlib.sha256).hexdigest()
    return {
        "Content-Type": "application/json",
        "X-CS-Timestamp": ts,
        "X-CS-Signature": sig,
    }


def _post_result(client: TestClient, job_id: str, payload: dict, token: str = "test-worker-secret"):
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
    return client.post(
        f"/api/internal/v1/jobs/{job_id}/result",
        content=body,
        headers=_hmac_headers(job_id, body, token=token),
    )


def test_result_endpoint_rejects_bare_bearer(worker_env):
    from app.main import app

    client = TestClient(app)
    job_id = "a" * 32
    res = client.post(
        f"/api/internal/v1/jobs/{job_id}/result",
        json={"status": "failed", "error": "x"},
        headers={"Authorization": "Bearer test-worker-secret"},
    )
    assert res.status_code == 401


def test_result_endpoint_rejects_bad_hmac(worker_env):
    from app.main import app

    client = TestClient(app)
    job_id = "a" * 32
    body = b'{"status":"failed","error":"x"}'
    headers = _hmac_headers(job_id, body, token="wrong-secret")
    res = client.post(
        f"/api/internal/v1/jobs/{job_id}/result",
        content=body,
        headers=headers,
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

    from app.main import app

    client = TestClient(app)
    posts: list[str] = []

    def fake_post(job_id_arg, path, payload):
        posts.append(path)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        res = client.post(
            path if path.startswith("/api") else f"/api{path}",
            content=body,
            headers=_hmac_headers(job_id_arg, body, token=env["token"]),
        )
        assert res.status_code < 400, res.text
        return res.json()

    monkeypatch.setattr(worker_client, "_post", fake_post)

    msg = QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=1)
    process_queue_message(msg)

    assert any(p.endswith("/result") for p in posts), posts
    progress = load_progress(job_id)
    assert progress is not None
    assert progress.status == "complete"
    report = load_report(job_id)
    assert report is not None
    assert report.ai_review == "complete"
    assert not env["store"].exists(job_meta_key(job_id))
    assert not env["store"].exists(job_source_key(job_id, source_name))


def test_fail_once_then_succeed_keeps_bundle(worker_env, monkeypatch):
    """Non-final failure must not POST terminal failed or delete the blob bundle."""
    from app.blob_store import job_meta_key, job_source_key
    from app.job_queue import QueueMessage
    from app.models import AuditTrail, Report
    from app.storage import load_progress
    from worker.run_job import WorkerJobError, process_queue_message
    from worker import client as worker_client

    env = worker_env
    job_id = "9" * 32
    source_name = "retry.pdf"
    src = env["tmp"] / source_name
    _tiny_pdf(src)
    meta = {
        "job_id": job_id,
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
        risk_score=1,
        ai_review="complete",
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
            filename=source_name,
            file_size_bytes=src.stat().st_size,
            file_type="application/pdf",
            sha256="a" * 64,
        ),
        artifact_urls=[],
    )

    calls = {"n": 0}

    def flaky_local(*a, **k):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("simulated pipeline failure")

    monkeypatch.setattr("worker.run_job.run_local_stage", flaky_local)
    monkeypatch.setattr("worker.run_job.run_gemini_stage", lambda *a, **k: fake_report)

    from app.main import app

    client = TestClient(app)
    result_posts: list[dict] = []

    def fake_post(job_id_arg, path, payload):
        if path.endswith("/result"):
            result_posts.append(payload)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        res = client.post(
            path if path.startswith("/api") else f"/api{path}",
            content=body,
            headers=_hmac_headers(job_id_arg, body, token=env["token"]),
        )
        assert res.status_code < 400, res.text
        return res.json()

    monkeypatch.setattr(worker_client, "_post", fake_post)

    msg1 = QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=1)
    with pytest.raises(WorkerJobError, match="simulated pipeline failure"):
        process_queue_message(msg1)

    # Non-final: no terminal result POST, bundle still present.
    assert result_posts == []
    assert env["store"].exists(job_meta_key(job_id))
    assert env["store"].exists(job_source_key(job_id, source_name))
    progress_after_fail = load_progress(job_id)
    assert progress_after_fail is None or progress_after_fail.status not in {"complete", "failed"}

    msg2 = QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=2)
    process_queue_message(msg2)

    assert any(p.get("status") == "complete" for p in result_posts)
    assert load_progress(job_id).status == "complete"
    assert not env["store"].exists(job_meta_key(job_id))


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
    res1 = _post_result(
        client,
        job_id,
        {"status": "complete", "report": report.model_dump(mode="json"), "message": "again"},
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
    res = _post_result(
        client,
        job_id,
        {"status": "failed", "error": "boom", "message": "Analysis failed."},
    )
    assert res.status_code == 200
    assert not env["store"].exists(job_meta_key(job_id))


def test_result_rejects_mismatched_firm(worker_env):
    from app.fact_memory.store import FactMemoryStore, reset_store
    from app.main import app
    from app.storage import create_job

    env = worker_env
    reset_store()
    db = env["tmp"] / "firm-mismatch.sqlite3"
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm_a = store.ensure_firm_for_email("a@example.com", name="Firm A")
    firm_b = store.ensure_firm_for_email("b@example.com", name="Firm B")
    job_id = "7" * 32
    store.upsert_analysis_job(
        job_id=job_id,
        firm_id=firm_a["firm_id"],
        user_id=firm_a["user_id"],
        matter_id=None,
        status="running",
        message="Scanning…",
    )
    create_job(job_id, "x.pdf")

    import app.fact_memory.store as store_mod

    store_mod._default_store = store

    client = TestClient(app)
    res = _post_result(
        client,
        job_id,
        {
            "status": "failed",
            "error": "x",
            "firm_id": firm_b["firm_id"],
            "user_id": firm_b["user_id"],
        },
    )
    assert res.status_code == 403
    assert "firm_id" in res.text.lower()
    reset_store()


def test_claim_honors_delay_sidecar(worker_env):
    from app.job_queue import QueueMessage, get_job_queue

    q = get_job_queue()
    msg = QueueMessage(job_id="3" * 32, bundle_prefix="jobs/" + "3" * 32, attempt=1)
    q.enqueue(msg)
    claimed = q.claim()
    assert claimed is not None
    q.release(claimed, delay_s=30.0)
    assert q.claim() is None
    assert q.ready_count() == 0


def test_local_blobs_404_outside_local_dev(worker_env, monkeypatch):
    from app.main import app
    import app.internal_jobs as internal_mod

    monkeypatch.setattr(internal_mod.settings, "app_env", "production")
    client = TestClient(app)
    res = client.put(
        "/api/internal/v1/local-blobs/jobs/x/source.pdf",
        content=b"%PDF",
        headers={"Authorization": "Bearer test-worker-secret"},
    )
    assert res.status_code == 404


def test_job_recovery_reenqueues_worker_bundle(worker_env):
    from app.blob_store import job_meta_key
    from app.fact_memory.store import FactMemoryStore, reset_store
    from app.job_queue import get_job_queue
    from app.job_recovery import recover_local_done_jobs

    env = worker_env
    reset_store()
    db = env["tmp"] / "recovery.sqlite3"
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("rec@example.com", name="Rec")
    job_id = "8" * 32
    store.upsert_analysis_job(
        job_id=job_id,
        firm_id=firm["firm_id"],
        user_id=firm["user_id"],
        status="local_done",
        message="Local done",
    )
    env["store"].put(job_meta_key(job_id), json.dumps({"job_id": job_id}).encode("utf-8"))

    import app.fact_memory.store as store_mod

    store_mod._default_store = store

    out = recover_local_done_jobs()
    assert out["requeued"] == 1
    assert get_job_queue().ready_count() == 1
    reset_store()


def test_crash_retries_then_dead_letters(worker_env, monkeypatch):
    from app.job_queue import LocalJobQueue, QueueMessage, get_job_queue, run_local_worker_loop
    from worker.run_job import WorkerJobError

    q = get_job_queue()
    job_id = "1" * 32
    q.enqueue(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=1))

    calls = {"n": 0}

    def boom(msg):
        calls["n"] += 1
        raise WorkerJobError("simulated crash")

    stop = threading.Event()
    _orig_release = LocalJobQueue.release

    def fast_release(self, message, *, delay_s=0):
        _orig_release(self, message, delay_s=0)

    monkeypatch.setattr(LocalJobQueue, "release", fast_release)

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
    assert not job_dest.exists()


def _seed_bundle(env, job_id: str, source_name: str = "doc.pdf", max_attempts: int = 3) -> None:
    from app.blob_store import job_meta_key, job_source_key

    src = env["tmp"] / source_name
    _tiny_pdf(src)
    meta = {
        "job_id": job_id,
        "persist": False,
        "source_name": source_name,
        "prior_snapshots": [],
        "submitted_by": "test",
        "attempt": 1,
        "max_attempts": max_attempts,
    }
    env["store"].put(job_source_key(job_id, source_name), src.read_bytes())
    env["store"].put(job_meta_key(job_id), json.dumps(meta).encode("utf-8"))


def test_final_attempt_failure_posts_single_terminal_result(worker_env, monkeypatch):
    """Real worker on its last attempt: exactly one 'failed' result, bundle deleted."""
    from app.blob_store import job_meta_key
    from app.job_queue import QueueMessage
    from app.storage import load_progress
    from worker import client as worker_client
    from worker.run_job import WorkerJobError, process_queue_message

    env = worker_env
    job_id = "f" * 32
    _seed_bundle(env, job_id, max_attempts=3)

    def always_fail(*a, **k):
        raise RuntimeError("pipeline exploded")

    monkeypatch.setattr("worker.run_job.run_local_stage", always_fail)

    from app.main import app

    client = TestClient(app)
    result_posts: list[dict] = []

    def fake_post(job_id_arg, path, payload):
        if path.endswith("/result"):
            result_posts.append(payload)
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        res = client.post(path, content=body, headers=_hmac_headers(job_id_arg, body, token=env["token"]))
        assert res.status_code < 400, res.text
        return res.json()

    monkeypatch.setattr(worker_client, "_post", fake_post)

    # Attempts 1 and 2: retryable, no terminal POST, bundle kept.
    for attempt in (1, 2):
        with pytest.raises(WorkerJobError):
            process_queue_message(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=attempt))
        assert result_posts == []
        assert env["store"].exists(job_meta_key(job_id))

    # Attempt 3 (final): exactly one terminal failed result, bundle deleted.
    with pytest.raises(WorkerJobError):
        process_queue_message(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=3))
    assert len(result_posts) == 1
    assert result_posts[0]["status"] == "failed"
    assert "pipeline exploded" in result_posts[0]["error"]
    assert result_posts[0]["attempt"] == 3
    assert load_progress(job_id).status == "failed"
    assert not env["store"].exists(job_meta_key(job_id))


def test_heartbeat_prevents_requeue_of_long_job(worker_env, monkeypatch):
    """A job running longer than the visibility timeout must not be re-claimed."""
    import app.job_queue as queue_mod
    from app.job_queue import QueueMessage, get_job_queue, run_local_worker_loop

    monkeypatch.setattr(queue_mod, "HEARTBEAT_INTERVAL_S", 0.05)
    q = get_job_queue()
    job_id = "e" * 32
    q.enqueue(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}"))

    stolen: list = []
    handled = threading.Event()
    stop = threading.Event()

    def slow_handler(msg):
        # Run well past a 0.3s visibility timeout while a second worker polls.
        deadline = time.time() + 0.8
        while time.time() < deadline:
            other = q.claim(visibility_timeout_s=0.3)
            if other is not None:
                stolen.append(other)
            time.sleep(0.05)
        handled.set()

    t = threading.Thread(
        target=run_local_worker_loop,
        args=(slow_handler,),
        kwargs={"idle_sleep_s": 0.02, "stop_event": stop},
        daemon=True,
    )
    t.start()
    assert handled.wait(5)
    stop.set()
    t.join(timeout=2)
    assert stolen == []
    inflight = list((worker_env["settings"].queue_dir / "inflight").glob("*.json"))
    assert inflight == []


def test_heartbeat_returns_false_after_claim_lost(worker_env):
    from app.job_queue import QueueMessage, get_job_queue

    q = get_job_queue()
    job_id = "d" * 32
    q.enqueue(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}"))
    msg = q.claim()
    assert q.heartbeat(msg) is True
    q.complete(msg)
    assert q.heartbeat(msg) is False
    assert not (worker_env["settings"].queue_dir / "inflight" / f"{job_id}.claimed").exists()


def test_delayed_release_is_never_claimable_early(worker_env):
    from app.job_queue import QueueMessage, get_job_queue

    q = get_job_queue()
    job_id = "c" * 32
    q.enqueue(QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}"))
    msg = q.claim()
    q.release(msg, delay_s=30)
    assert q.claim() is None
    ready = worker_env["settings"].queue_dir / "ready" / f"{job_id}.json"
    assert json.loads(ready.read_text())["attempt"] == 2
    assert not list((worker_env["settings"].queue_dir / "ready").glob("*.tmp"))


def test_worker_client_sends_no_bearer_secret(worker_env, monkeypatch):
    import urllib.request
    from worker import client as worker_client

    seen: dict = {}

    class _Resp:
        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"ok": true}'

    def fake_urlopen(req, timeout=None):
        seen["headers"] = {k.lower(): v for k, v in req.header_items()}
        return _Resp()

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    worker_client.post_progress("a" * 32, status="running")
    headers = seen["headers"]
    assert "authorization" not in headers
    assert "test-worker-secret" not in json.dumps(headers)
    assert headers.get("x-cs-signature")
    assert headers.get("x-cs-timestamp")


def test_post_result_retries_transient_then_succeeds(worker_env, monkeypatch):
    from worker import client as worker_client

    monkeypatch.setattr(worker_client, "RESULT_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    calls = {"n": 0}

    def flaky(job_id, path, payload):
        calls["n"] += 1
        if calls["n"] < 3:
            raise RuntimeError("callback HTTP 503: unavailable")
        return {"ok": True}

    monkeypatch.setattr(worker_client, "_post", flaky)
    assert worker_client.post_result("b" * 32, {"status": "failed"}) == {"ok": True}
    assert calls["n"] == 3


def test_post_result_does_not_retry_auth_errors(worker_env, monkeypatch):
    from worker import client as worker_client

    monkeypatch.setattr(worker_client, "RESULT_RETRY_DELAYS_S", (0.0, 0.0, 0.0))
    calls = {"n": 0}

    def unauthorized(job_id, path, payload):
        calls["n"] += 1
        raise RuntimeError("callback HTTP 401: Invalid worker signature.")

    monkeypatch.setattr(worker_client, "_post", unauthorized)
    with pytest.raises(RuntimeError, match="401"):
        worker_client.post_result("b" * 32, {"status": "failed"})
    assert calls["n"] == 1
