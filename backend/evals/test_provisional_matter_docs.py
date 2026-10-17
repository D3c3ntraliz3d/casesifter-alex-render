"""Accepted uploads must remain visible before Document persist completes."""

from __future__ import annotations

from app.fact_memory.matters_api import _overlay_open_scans
from app.fact_memory.store import FactMemoryStore, reset_store


def test_open_jobs_appear_on_matter_overlay_and_case_tile_count(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "provisional_docs.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("firm@example.com", name="Firm")
    matter = store.create_matter(firm["firm_id"], name="Matter", user_id=firm["user_id"])

    store.upsert_analysis_job(
        job_id="job-open-1",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256="a" * 64,
        status="queued",
        original_filename="10 - AFFIRMATION.pdf",
        display_name="Affirmation",
        doc_kind="affirmation",
    )
    store.upsert_analysis_job(
        job_id="job-open-2",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256="b" * 64,
        status="running",
        original_filename="11 - EXHIBIT.pdf",
        display_name="Exhibit",
        doc_kind="exhibit",
    )

    docs = store.list_documents(firm["firm_id"], matter["id"])
    assert docs == []

    listed = _overlay_open_scans(store, firm["firm_id"], matter["id"], docs)
    assert len(listed) == 2
    by_sha = {d["sha256"]: d for d in listed}
    assert by_sha["a" * 64]["display_name"] == "Affirmation"
    assert by_sha["a" * 64]["pending_upload"] is True
    assert by_sha["b" * 64]["status"] == "scanning"

    matters = store.list_matters(firm["firm_id"])
    row = next(m for m in matters if m["id"] == matter["id"])
    assert row["document_count"] == 2

    reset_store()


def test_delete_document_detaches_jobs_so_provisional_does_not_return(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "detach_jobs.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("firm2@example.com", name="Firm")
    matter = store.create_matter(firm["firm_id"], name="Matter", user_id=firm["user_id"])

    persisted = store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-done",
        sha256="c" * 64,
        original_filename="done.pdf",
        display_name="Done",
        doc_type="exhibit",
        filed_date=None,
        page_count=1,
        entities={},
        facts=[],
        findings=[],
        text_fingerprint="fp",
        text_excerpt="x",
        page_offsets=[],
        user_id=firm["user_id"],
        report={"risk_level": "green", "risk_score": 0},
    )
    store.upsert_analysis_job(
        job_id="job-done",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        document_id=persisted["document_id"],
        sha256="c" * 64,
        status="complete",
        original_filename="done.pdf",
        display_name="Done",
    )

    assert store.delete_document(firm["firm_id"], persisted["document_id"], user_id=firm["user_id"])
    job = store.get_analysis_job("job-done")
    assert job["matter_id"] is None
    assert job["document_id"] is None

    listed = _overlay_open_scans(store, firm["firm_id"], matter["id"], [])
    assert listed == []

    reset_store()
