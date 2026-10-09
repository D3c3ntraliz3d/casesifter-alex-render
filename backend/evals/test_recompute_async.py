"""Async recompute runs in a child Process; /api/health stays responsive."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient

from app.fact_memory.store import FactMemoryStore, reset_store
from app.recompute_jobs import get_recompute_job, start_recompute_job


_NOTE = (
    "The Note provides for interest at the rate of 5.125% per annum.\n"
    "Upon an Event of Default, interest at the rate of TWENTY-FOUR PERCENT (24%) "
    "per annum shall accrue until the Default is cured.\n"
)


def _seed_matter(tmp_path, monkeypatch, *, n_docs: int):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-recompute-async")
    db = tmp_path / "recompute-async.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    monkeypatch.setenv("APP_MODE", "analysis")
    monkeypatch.setenv("ANALYSIS_API_KEYS", "test-recompute-health")
    data_dir = tmp_path / "data"
    data_dir.mkdir(exist_ok=True)
    monkeypatch.setenv("DATA_DIR", str(data_dir))
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("async@example.com", name="Async")
    matter = store.create_matter(firm["firm_id"], name="async-matter", user_id=firm["user_id"])
    for i in range(n_docs):
        # Unique 64-char hex sha per doc.
        sha = f"{i:064x}"
        store.persist_scan(
            firm_id=firm["firm_id"],
            matter_id=matter["id"],
            job_id=f"job-async-{i}",
            sha256=sha,
            original_filename=f"note-{i}.pdf",
            display_name=f"Note {i}",
            doc_type="exhibit",
            filed_date=None,
            page_count=1,
            entities={},
            facts=[{"key": "stated_interest_rate", "value": 5.0, "display": "5%"}],
            findings=[],
            text_fingerprint=f"fp-async-{i}",
            text_excerpt=_NOTE,
            page_offsets=[],
            report={"risk_level": "green", "risk_score": 0},
            user_id=firm["user_id"],
            force=True,
        )
    return store, firm, matter, data_dir


def test_start_recompute_job_completes_in_process(tmp_path, monkeypatch):
    store, firm, matter, data_dir = _seed_matter(tmp_path, monkeypatch, n_docs=3)
    from app.config import settings as cfg

    monkeypatch.setattr(cfg, "data_dir", data_dir)
    row = start_recompute_job(
        firm_id=firm["firm_id"], matter_id=matter["id"], user_id=firm["user_id"]
    )
    assert row["job_id"]
    assert row["status"] in {"queued", "running"}
    job_id = row["job_id"]

    deadline = time.time() + 180
    final = None
    while time.time() < deadline:
        final = get_recompute_job(job_id)
        if final and final.get("status") in {"complete", "failed"}:
            break
        time.sleep(0.2)

    assert final is not None, "recompute never produced status"
    assert final["status"] == "complete", final
    assert final.get("updated", 0) >= 1
    assert isinstance(final.get("before"), list)
    assert isinstance(final.get("after"), list)


def test_health_responsive_during_58_doc_recompute(tmp_path, monkeypatch):
    """Hit /api/health in a loop while a 58-doc recompute runs — must stay <1s."""
    store, firm, matter, data_dir = _seed_matter(tmp_path, monkeypatch, n_docs=58)

    from app.config import Settings
    import app.config as config_mod
    import importlib
    import app.main as main_mod

    settings = Settings(
        app_mode="analysis",
        analysis_api_keys="test-recompute-health",
        fact_memory_master_key="unit-test-master-key-recompute-async",
        data_dir=data_dir,
    )
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr("app.config.settings", settings)
    monkeypatch.setattr("app.main.settings", settings)
    importlib.reload(main_mod)
    client = TestClient(main_mod.app)

    # Warm health once (import / lifespan).
    warm = client.get("/api/health")
    assert warm.status_code == 200

    row = start_recompute_job(
        firm_id=firm["firm_id"], matter_id=matter["id"], user_id=firm["user_id"]
    )
    job_id = row["job_id"]
    samples: list[float] = []
    deadline = time.time() + 180
    final = None
    while time.time() < deadline:
        t0 = time.perf_counter()
        res = client.get("/api/health")
        elapsed = time.perf_counter() - t0
        assert res.status_code == 200, res.text
        assert elapsed < 1.0, f"health took {elapsed:.3f}s"
        samples.append(elapsed)
        final = get_recompute_job(job_id)
        if final and final.get("status") in {"complete", "failed"}:
            break
        time.sleep(0.05)

    assert final is not None, "recompute never finished"
    assert final["status"] == "complete", final
    assert samples, "no health samples collected"
    assert max(samples) < 1.0
    assert final.get("updated", 0) >= 50


def test_restart_interrupt_resume_keeps_attempts(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-restart")
    db = tmp_path / "restart-attempts.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("restart@example.com", name="R")
    matter = store.create_matter(firm["firm_id"], name="m", user_id=firm["user_id"])
    sha = "c" * 64
    store.upsert_analysis_job(
        job_id="job-r1",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256=sha,
        status="running",
        attempts=2,
        message="Scanning",
    )
    n = store.mark_active_jobs_interrupted()
    assert n == 1
    row = store.get_analysis_job("job-r1")
    assert row["error"] == "interrupted_by_restart"
    assert row["attempts"] == 2
    assert row["restart_interruptions"] == 1

    # Simulating create-job resume: upsert new queued job with same attempt count.
    store.upsert_analysis_job(
        job_id="job-r2",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256=sha,
        status="queued",
        attempts=2,
        message="Resumed after restart",
    )
    assert store.max_attempts_for_sha(
        firm_id=firm["firm_id"], matter_id=matter["id"], sha256=sha
    ) == 2
    # Mark r2 running then interrupt again → quarantine (2nd running-at-restart).
    store.update_analysis_job("job-r2", status="running", message="Scanning again")
    store.set_restart_interruptions("job-r2", 1)
    n2 = store.mark_active_jobs_interrupted()
    assert n2 == 1
    q = store.get_analysis_job("job-r2")
    assert q["error"] == "restart_quarantine"
    assert q["restart_interruptions"] >= 2
    assert "crashing" in (q["message"] or "").lower()
    retryable = store.list_retryable_failed_jobs(
        firm_id=firm["firm_id"], matter_id=matter["id"], max_attempts=3
    )
    assert not any(r["sha256"] == sha for r in retryable)
    exhausted = store.list_exhausted_jobs(
        firm_id=firm["firm_id"], matter_id=matter["id"], max_attempts=3
    )
    assert any(r["sha256"] == sha for r in exhausted)
