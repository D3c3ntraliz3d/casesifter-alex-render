"""Async recompute-facts job returns 202 and completes off the request path."""

from __future__ import annotations

import time

from app.fact_memory.store import FactMemoryStore, reset_store
from app.recompute_jobs import get_recompute_job, start_recompute_job


def test_start_recompute_job_completes(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-recompute-async")
    db = tmp_path / "recompute-async.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("async@example.com", name="Async")
    matter = store.create_matter(firm["firm_id"], name="async-matter", user_id=firm["user_id"])
    store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-async-1",
        sha256="b" * 64,
        original_filename="note.pdf",
        display_name="Note",
        doc_type="exhibit",
        filed_date=None,
        page_count=1,
        entities={},
        facts=[{"key": "stated_interest_rate", "value": 5.0, "display": "5%"}],
        findings=[],
        text_fingerprint="fp-async",
        text_excerpt="The Note provides for interest at the rate of 5.125% per annum.\n",
        page_offsets=[],
        report={"risk_level": "green", "risk_score": 0},
        user_id=firm["user_id"],
        force=True,
    )

    row = start_recompute_job(
        firm_id=firm["firm_id"], matter_id=matter["id"], user_id=firm["user_id"]
    )
    assert row["job_id"]
    assert row["status"] in {"queued", "running"}
    job_id = row["job_id"]

    deadline = time.time() + 30
    final = None
    while time.time() < deadline:
        final = get_recompute_job(job_id)
        if final and final.get("status") in {"complete", "failed"}:
            break
        time.sleep(0.05)

    assert final is not None
    assert final["status"] == "complete", final
    assert final.get("updated", 0) >= 1
    assert isinstance(final.get("before"), list)
    assert isinstance(final.get("after"), list)
