"""Scale Phase 3: S3-compatible blob store (R2/S3) + shared Redis queue."""

from __future__ import annotations

import json
import threading
import time

import fakeredis
import pytest
from moto import mock_aws

from evals.test_scan_worker_phase1 import _hmac_headers, _tiny_pdf, worker_env  # noqa: F401


@pytest.fixture()
def s3_store():
    import boto3

    from app.blob_store import S3BlobStore

    with mock_aws():
        from botocore.config import Config

        # Same signing as production (R2 requires SigV4).
        client = boto3.client("s3", region_name="us-east-1", config=Config(signature_version="s3v4"))
        client.create_bucket(Bucket="casesifter-test")
        yield S3BlobStore(client=client, bucket="casesifter-test")


@pytest.fixture()
def redis_queue():
    from app.job_queue import RedisJobQueue

    r = fakeredis.FakeRedis(decode_responses=True)
    return RedisJobQueue(client=r, prefix="test:scan")


# --- S3BlobStore ------------------------------------------------------------


def test_s3_put_get_exists_delete(s3_store, tmp_path):
    s3_store.put("jobs/abc/job.json", b'{"a":1}', content_type="application/json")
    assert s3_store.exists("jobs/abc/job.json")
    assert s3_store.get("jobs/abc/job.json") == b'{"a":1}'

    src = tmp_path / "big.pdf"
    src.write_bytes(b"%PDF" + b"x" * 200_000)
    with src.open("rb") as fh:
        s3_store.put("jobs/abc/big.pdf", fh)
    out = s3_store.get_file("jobs/abc/big.pdf", tmp_path / "dl" / "big.pdf")
    assert out.read_bytes() == src.read_bytes()

    assert s3_store.delete_prefix("jobs/abc") == 2
    assert not s3_store.exists("jobs/abc/job.json")
    assert s3_store.delete("jobs/abc/job.json") is True  # missing is success


def test_s3_missing_raises_file_not_found(s3_store, tmp_path):
    with pytest.raises(FileNotFoundError):
        s3_store.get("jobs/nope/job.json")
    with pytest.raises(FileNotFoundError):
        s3_store.get_file("jobs/nope/a.pdf", tmp_path / "a.pdf")


def test_s3_prefix_delete_is_scoped(s3_store):
    s3_store.put("jobs/abc/a.pdf", b"1")
    s3_store.put("jobs/abcd/a.pdf", b"2")  # shares a string prefix, different job
    assert s3_store.delete_prefix("jobs/abc") == 1
    assert s3_store.exists("jobs/abcd/a.pdf")


def test_s3_rejects_path_escape(s3_store):
    with pytest.raises(ValueError):
        s3_store.put("jobs/../secrets", b"x")


def test_s3_presigned_urls(s3_store):
    up = s3_store.signed_upload_url("jobs/abc/a.pdf", content_type="application/pdf")
    down = s3_store.signed_download_url("jobs/abc/a.pdf")
    assert "casesifter-test" in up and "X-Amz-Signature=" in up
    assert "casesifter-test" in down and "X-Amz-Signature=" in down


# --- RedisJobQueue ------------------------------------------------------------


def _msg(job_id: str, attempt: int = 1):
    from app.job_queue import QueueMessage

    return QueueMessage(job_id=job_id, bundle_prefix=f"jobs/{job_id}", attempt=attempt)


def test_redis_enqueue_claim_complete(redis_queue):
    q = redis_queue
    q.enqueue(_msg("a" * 32))
    assert q.ready_count() == 1
    m = q.claim()
    assert m.job_id == "a" * 32 and m.claim_token
    assert q.claim() is None
    assert q.inflight_count() == 1
    q.complete(m)
    assert q.inflight_count() == 0 and q.ready_count() == 0


def test_redis_release_bumps_attempt_and_honors_delay(redis_queue):
    q = redis_queue
    q.enqueue(_msg("b" * 32))
    m = q.claim()
    q.release(m, delay_s=30)
    assert q.claim() is None  # delayed
    q.r.zadd(q.k_ready, {"b" * 32: time.time() - 1})  # fast-forward
    m2 = q.claim()
    assert m2.attempt == 2


def test_redis_expired_claim_requeued_and_old_holder_is_powerless(redis_queue):
    """Worker A stalls; B re-claims. A's late complete/release/fail must not touch B's claim."""
    q = redis_queue
    q.enqueue(_msg("c" * 32))
    a = q.claim(visibility_timeout_s=1)
    q.r.zadd(q.k_inflight, {"c" * 32: time.time() - 10})  # A stopped heartbeating
    b = q.claim(visibility_timeout_s=1)
    assert b is not None and b.claim_token != a.claim_token

    assert q.heartbeat(a) is False
    q.complete(a)
    q.release(a, delay_s=0)
    q.fail(a, reason="late")
    assert q.inflight_count() == 1 and q.dead_count() == 0 and q.ready_count() == 0

    assert q.heartbeat(b) is True
    q.complete(b)
    assert q.inflight_count() == 0


def test_redis_heartbeat_keeps_claim(redis_queue):
    q = redis_queue
    q.enqueue(_msg("d" * 32))
    m = q.claim(visibility_timeout_s=1)
    q.r.zadd(q.k_inflight, {"d" * 32: time.time() - 10})
    assert q.heartbeat(m) is True  # renews the score to now
    assert q.claim(visibility_timeout_s=1) is None


def test_redis_fail_dead_letters(redis_queue):
    q = redis_queue
    q.enqueue(_msg("e" * 32))
    m = q.claim()
    q.fail(m, reason="boom")
    assert q.dead_count() == 1
    dead = json.loads(q.r.hget(q.k_dead, "e" * 32))
    assert dead["failed_reason"] == "boom" and "claim_token" not in dead


def test_redis_enqueue_skips_job_in_flight(redis_queue):
    """Boot recovery re-enqueue must not create a second copy of a running job."""
    q = redis_queue
    q.enqueue(_msg("f" * 32))
    m = q.claim()
    q.enqueue(_msg("f" * 32))
    assert q.ready_count() == 0
    q.complete(m)


def test_redis_token_not_persisted(redis_queue):
    q = redis_queue
    q.enqueue(_msg("1" * 32))
    m = q.claim()
    q.release(m)
    assert "claim_token" not in q.r.hget(q.k_msgs, "1" * 32)


def test_two_workers_never_run_same_job(redis_queue):
    from app.job_queue import QueueMessage

    q = redis_queue
    for i in range(20):
        q.enqueue(QueueMessage(job_id=f"{i:032d}", bundle_prefix="x"))
    seen: list[str] = []
    lock = threading.Lock()

    def drain():
        while True:
            m = q.claim()
            if m is None:
                return
            with lock:
                seen.append(m.job_id)
            q.complete(m)

    ts = [threading.Thread(target=drain) for _ in range(4)]
    for t in ts:
        t.start()
    for t in ts:
        t.join()
    assert sorted(seen) == sorted(f"{i:032d}" for i in range(20))


# --- End to end: R2/S3 bundle + Redis queue through the real worker ----------


def test_worker_round_trip_on_s3_and_redis(worker_env, s3_store, redis_queue, monkeypatch):  # noqa: F811
    from datetime import datetime, timezone

    import app.blob_store as blob_mod
    import app.job_queue as queue_mod
    from app.blob_store import job_meta_key, job_source_key
    from app.job_queue import run_local_worker_loop
    from app.models import AuditTrail, Report
    from app.storage import load_progress
    from fastapi.testclient import TestClient
    from worker import client as worker_client
    from worker.run_job import process_queue_message

    blob_mod.reset_blob_store_for_tests(s3_store)
    queue_mod.reset_job_queue_for_tests(redis_queue)

    env = worker_env
    job_id = "7" * 32
    name = "exhibit.pdf"
    src = env["tmp"] / name
    _tiny_pdf(src)
    s3_store.put(job_source_key(job_id, name), src.read_bytes())
    s3_store.put(
        job_meta_key(job_id),
        json.dumps({"job_id": job_id, "source_name": name, "persist": False, "max_attempts": 3}).encode(),
    )
    redis_queue.enqueue(_msg(job_id))

    report = Report(
        job_id=job_id, risk_level="green", risk_score=1, ai_review="complete",
        executive_summary="ok", top_findings=[], recommended_actions=[], findings=[],
        timeline=[], page_dimensions=[], fonts=[], comparison={}, modules=[],
        audit=AuditTrail(
            scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc), analysis_version="t",
            modules_run=[], submitted_by="t", filename=name,
            file_size_bytes=src.stat().st_size, file_type="application/pdf", sha256="b" * 64,
        ),
        artifact_urls=[],
    )
    monkeypatch.setattr("worker.run_job.run_local_stage", lambda *a, **k: None)
    monkeypatch.setattr("worker.run_job.run_gemini_stage", lambda *a, **k: report)

    from app.main import app

    client = TestClient(app)

    def fake_post(job_id_arg, path, payload):
        body = json.dumps(payload, separators=(",", ":")).encode("utf-8")
        res = client.post(path, content=body, headers=_hmac_headers(job_id_arg, body, token=env["token"]))
        assert res.status_code < 400, res.text
        return res.json()

    monkeypatch.setattr(worker_client, "_post", fake_post)

    done = threading.Event()

    def handler(msg):
        process_queue_message(msg)
        done.set()

    stop = threading.Event()
    t = threading.Thread(
        target=run_local_worker_loop, args=(handler,),
        kwargs={"idle_sleep_s": 0.02, "stop_event": stop}, daemon=True,
    )
    t.start()
    assert done.wait(10)
    stop.set()
    t.join(timeout=3)

    assert load_progress(job_id).status == "complete"
    assert not s3_store.exists(job_meta_key(job_id))
    assert not s3_store.exists(job_source_key(job_id, name))
    assert redis_queue.inflight_count() == 0 and redis_queue.ready_count() == 0


def test_worker_writes_reports_without_master_key_in_production(worker_env, monkeypatch):  # noqa: F811
    """Regression: production worker has no FACT_MEMORY_MASTER_KEY; the pipeline's
    update_job(report=...) must still succeed using the process-local key."""
    from datetime import datetime, timezone

    import app.storage as storage_mod
    from app.fact_memory.encryption import master_fernet
    from app.models import AuditTrail, Report
    from app.storage import create_job, load_report, update_job

    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.delenv("FACT_MEMORY_MASTER_KEY", raising=False)
    master_fernet.cache_clear()
    monkeypatch.setattr(storage_mod, "_report_fernet_override", None)

    job_id = "8" * 32
    create_job(job_id, "x.pdf")
    report = Report(
        job_id=job_id, risk_level="green", risk_score=1, ai_review="complete",
        executive_summary="ok", top_findings=[], recommended_actions=[], findings=[],
        timeline=[], page_dimensions=[], fonts=[], comparison={}, modules=[],
        audit=AuditTrail(
            scanned_at=datetime(2026, 1, 1, tzinfo=timezone.utc), analysis_version="t",
            modules_run=[], submitted_by="t", filename="x.pdf", file_size_bytes=1,
            file_type="application/pdf", sha256="c" * 64,
        ),
        artifact_urls=[],
    )

    # Without the worker key: fails closed exactly as it did in production.
    with pytest.raises(RuntimeError, match="FACT_MEMORY_MASTER_KEY"):
        update_job(job_id, status="complete", report=report)

    storage_mod.use_ephemeral_report_key()
    update_job(job_id, status="complete", report=report)
    raw = (storage_mod.job_dir(job_id) / "report.json").read_text()
    assert raw.startswith(storage_mod._REPORT_ENC_PREFIX)  # still encrypted at rest
    assert "executive_summary" not in raw
    assert load_report(job_id).executive_summary == "ok"
    master_fernet.cache_clear()


def test_worker_main_enables_ephemeral_report_key(monkeypatch):
    """worker.main must switch on the process-local key before polling."""
    import app.storage as storage_mod
    import worker.main as wm

    monkeypatch.setattr(storage_mod, "_report_fernet_override", None)
    monkeypatch.setattr("app.worker_auth.require_worker_token_configured", lambda: None)
    probed = {"n": 0}

    def _probe():
        probed["n"] += 1

    monkeypatch.setattr("app.gemini_tasks.startup_validate_and_probe", _probe)

    def stop_loop(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr("worker.pool.run_worker_pool", stop_loop)
    wm.main()
    assert storage_mod._report_fernet_override is not None
    assert probed["n"] == 1, "scan worker must run models.get / TASK_* validation on startup"
