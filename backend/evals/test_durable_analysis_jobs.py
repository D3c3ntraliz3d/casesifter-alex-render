"""Durable analysis_jobs ledger survives ephemeral progress.json loss."""

from __future__ import annotations

from app.fact_memory.store import FactMemoryStore, reset_store


def test_mark_interrupted_and_get(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "durable_jobs.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("ops@example.com", name="Ops")
    matter = store.create_matter(firm["firm_id"], name="Matter", user_id=firm["user_id"])

    store.upsert_analysis_job(
        job_id="job-queued",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256="abc123",
        status="queued",
        message="Queued for analysis.",
    )
    store.upsert_analysis_job(
        job_id="job-running",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256="def456",
        status="running",
        message="Scanning…",
    )
    store.upsert_analysis_job(
        job_id="job-done",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256="ghi789",
        status="complete",
        message="Done.",
    )

    n = store.mark_active_jobs_interrupted()
    assert n == 2

    q = store.get_analysis_job("job-queued")
    r = store.get_analysis_job("job-running")
    d = store.get_analysis_job("job-done")
    assert q["status"] == "interrupted"
    assert r["status"] == "interrupted"
    assert d["status"] == "complete"
    assert q["error"] == "interrupted_by_restart"

    store.update_analysis_job("job-running", status="complete", message="Recovered.")
    assert store.get_analysis_job("job-running")["status"] == "complete"
    reset_store()
