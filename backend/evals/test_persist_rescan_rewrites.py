"""A finished rescan must rewrite facts and scanned_at, not only the report blob."""

from __future__ import annotations

import pytest

from app.fact_memory.store import FactMemoryStore, reset_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "fact_memory_rescan.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    s = FactMemoryStore(database_url=f"sqlite:///{db}")
    yield s
    reset_store()


def test_rescan_with_report_rewrites_facts_and_scanned_at(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("rescan@example.com")
    matter = store.create_matter(firm["firm_id"], name="Rescan", user_id=firm["user_id"])
    first = store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-1",
        sha256="same-sha",
        original_filename="a.pdf",
        display_name="A",
        doc_type="exhibit",
        filed_date="2020-01-02",
        page_count=1,
        entities={},
        facts=[{"key": "old", "value": "before", "display": "before"}],
        findings=[],
        text_fingerprint="fp1",
        text_excerpt="before",
        page_offsets=[{"page": 1, "start": 0}],
        report={"modules": [{"name": "metadata", "status": "ok"}], "findings": []},
        user_id=firm["user_id"],
    )
    doc_id = first["document_id"]
    docs = store.list_documents(firm["firm_id"], matter["id"])
    assert len(docs) == 1
    assert docs[0]["filed_date"] == "2020-01-02"
    first_scanned = docs[0]["scanned_at"]
    assert first_scanned
    assert docs[0]["last_scanned_at"] == first_scanned

    second = store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-2",
        sha256="same-sha",
        original_filename="a.pdf",
        display_name="A",
        doc_type="exhibit",
        filed_date=None,  # omit — keep the user-entered date
        page_count=1,
        entities={},
        facts=[{"key": "new", "value": "after", "display": "after"}],
        findings=[],
        text_fingerprint="fp2",
        text_excerpt="after",
        page_offsets=[{"page": 1, "start": 0}],
        report={
            "modules": [
                {"name": "metadata", "status": "ok"},
                {"name": "image_forensics", "status": "ok"},
            ],
            "findings": [],
            "comparison": {"scan_profile": "forensic"},
        },
        user_id=firm["user_id"],
    )
    assert second["document_id"] == doc_id
    assert second.get("deduped") is not True

    docs = store.list_documents(firm["firm_id"], matter["id"])
    assert len(docs) == 1
    assert docs[0]["filed_date"] == "2020-01-02"
    assert docs[0]["scan_job_id"] == "job-2"
    assert docs[0]["scanned_at"] >= first_scanned
    assert docs[0]["last_scanned_at"] == docs[0]["scanned_at"]
    snaps = store.list_prior_snapshots(firm["firm_id"], matter["id"])
    assert len(snaps) == 1
    keys = {f.get("key") or f.get("attribute") for f in (snaps[0].get("facts") or [])}
    assert "new" in keys
    assert "old" not in keys
