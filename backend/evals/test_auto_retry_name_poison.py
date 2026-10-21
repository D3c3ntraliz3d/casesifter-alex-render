"""Auto-retry must not overwrite real titles with retry-<sha> placeholders."""

from __future__ import annotations

import pytest

from app.auto_title import is_synthetic_auto_retry_name, propose_document_title
from app.fact_memory.store import FactMemoryStore, reset_store


@pytest.fixture()
def store(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-please-rotate")
    db = tmp_path / "fact_memory_retry_name.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    s = FactMemoryStore(database_url=f"sqlite:///{db}")
    yield s
    reset_store()


def test_synthetic_auto_retry_name_detection():
    assert is_synthetic_auto_retry_name("retry-a1b2c3d4e5f6.pdf")
    assert is_synthetic_auto_retry_name("retry a1b2c3d4e5f6")
    assert is_synthetic_auto_retry_name("Retry_A1B2C3D4E5F6.PDF")
    assert not is_synthetic_auto_retry_name("retry_motion.pdf")
    assert not is_synthetic_auto_retry_name("Plaintiff Retry Affidavit.pdf")
    assert not is_synthetic_auto_retry_name("Affidavit.pdf")
    assert not is_synthetic_auto_retry_name("")


def test_auto_title_turns_retry_sha_into_retry_word():
    title, _kind = propose_document_title("retry-a1b2c3d4e5f6.pdf")
    assert title.lower().startswith("retry")


def test_persist_scan_keeps_real_name_over_synthetic_retry(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("retry-name@example.com")
    matter = store.create_matter(firm["firm_id"], name="Retry Name", user_id=firm["user_id"])
    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-1",
        sha256="abc123",
        original_filename="Affidavit_of_Service.pdf",
        display_name="Affidavit of Service",
        doc_type="proof_of_service",
        filed_date=None,
        page_count=1,
        entities={},
        facts=[],
        findings=[],
        text_fingerprint="fp1",
        text_excerpt="service",
        page_offsets=[],
        report={"modules": [], "findings": []},
        user_id=firm["user_id"],
    )
    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-2",
        sha256="abc123",
        original_filename="retry-deadbeefcafe.pdf",
        display_name="retry deadbeefcafe",
        doc_type="proof_of_service",
        filed_date=None,
        page_count=1,
        entities={},
        facts=[],
        findings=[],
        text_fingerprint="fp2",
        text_excerpt="service",
        page_offsets=[],
        report={"modules": [], "findings": []},
        user_id=firm["user_id"],
    )
    docs = store.list_documents(firm["firm_id"], matter["id"])
    assert len(docs) == 1
    assert docs[0]["original_filename"] == "Affidavit_of_Service.pdf"
    assert docs[0]["display_name"] == "Affidavit of Service"


def test_heal_synthetic_names_from_older_jobs(store: FactMemoryStore):
    firm = store.ensure_firm_for_email("heal-retry@example.com")
    matter = store.create_matter(firm["firm_id"], name="Heal", user_id=firm["user_id"])
    sha = "f" * 64
    store.upsert_analysis_job(
        job_id="old-job",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256=sha,
        status="failed",
        original_filename="Complaint.pdf",
        display_name="Complaint",
        attempts=1,
    )
    store.upsert_analysis_job(
        job_id="retry-job",
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        sha256=sha,
        status="complete",
        original_filename="retry-aaaaaaaaaaaa.pdf",
        display_name="retry aaaaaaaaaaaa",
        attempts=2,
    )
    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="retry-job",
        sha256=sha,
        original_filename="retry-aaaaaaaaaaaa.pdf",
        display_name="retry aaaaaaaaaaaa",
        doc_type="complaint",
        filed_date=None,
        page_count=1,
        entities={},
        facts=[],
        findings=[],
        text_fingerprint="fp",
        text_excerpt="complaint",
        page_offsets=[],
        report={"modules": [], "findings": []},
        user_id=firm["user_id"],
    )
    # First persist had no prior doc, so synthetic names stuck — heal recovers them.
    n = store.heal_synthetic_auto_retry_names(
        firm["firm_id"], matter["id"], user_id=firm["user_id"]
    )
    assert n == 1
    docs = store.list_documents(firm["firm_id"], matter["id"])
    assert docs[0]["original_filename"] == "Complaint.pdf"
    assert docs[0]["display_name"] == "Complaint"
