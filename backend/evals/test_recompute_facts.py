"""Recompute facts from stored excerpts — real store, not mocks."""

from __future__ import annotations

from app.fact_memory.store import FactMemoryStore, reset_store


def test_recompute_facts_from_excerpts_updates_facts_and_stales_conflicts(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-recompute")
    db = tmp_path / "recompute.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("ops@example.com", name="Ops")
    matter = store.create_matter(firm["firm_id"], name="tesst", user_id=firm["user_id"])

    note_text = (
        "The Note provides for interest at the rate of 5.125% per annum.\n"
        "Upon an Event of Default, interest at the rate of TWENTY-FOUR PERCENT (24%) "
        "per annum shall accrue until the Default is cured.\n"
        "Prior mortgage principal amount $417,000.00.\n"
        "Gap mortgage principal amount $138,000.00.\n"
        "Consolidated principal amount $555,000.00.\n"
    )
    persisted = store.persist_scan(
        firm_id=firm["firm_id"],
        matter_id=matter["id"],
        job_id="job-recompute-1",
        sha256="a" * 64,
        original_filename="note.pdf",
        display_name="Exhibit Note",
        doc_type="exhibit",
        filed_date=None,
        page_count=2,
        entities={},
        facts=[
            {
                "key": "stated_interest_rate",
                "value": 5.125,
                "display": "5.125%",
                "rate_purpose": "note",
                "quote": "interest at the rate of 5.125%",
            },
            {
                "key": "stated_interest_rate",
                "value": 24.0,
                "display": "24%",
                "rate_purpose": "note",  # stale wrong purpose — recompute should fix
                "quote": "TWENTY-FOUR PERCENT (24%)",
            },
        ],
        findings=[
            {
                "id": "f1",
                "finding_type": "fact_conflict",
                "severity": "high",
                "title": "RED FLAG: Stated interest rate conflicts",
                "explanation": "5.125 vs 24",
                "evidence": {"analysis_type": "fact_conflict", "field": "stated_interest_rate"},
            }
        ],
        text_fingerprint="fp1",
        text_excerpt=note_text,
        page_offsets=[],
        report={"risk_level": "red", "risk_score": 0},
        user_id=firm["user_id"],
        force=True,
    )
    assert persisted.get("persisted")

    # Seed an open fact_conflict finding (FindingRow has no document_id column).
    from app.fact_memory import encryption as enc
    from app.fact_memory.models import FindingRow
    import uuid

    with store.session() as s:
        fernet = store._fernet(s, firm["firm_id"])
        s.add(
            FindingRow(
                id=str(uuid.uuid4()),
                matter_id=matter["id"],
                firm_id=firm["firm_id"],
                run_id=None,
                finding_type="fact_conflict",
                severity="high",
                confidence=88,
                description_enc=enc.encrypt_text(fernet, "rate conflict"),
                title_enc=enc.encrypt_text(fernet, "RED FLAG: rates"),
                fact_ids_json="[]",
                evidence_enc=enc.encrypt_json(
                    fernet, {"analysis_type": "fact_conflict", "field": "stated_interest_rate"}
                ),
                status="open",
            )
        )
        s.commit()

    result = store.recompute_facts_from_excerpts(
        firm["firm_id"], matter["id"], user_id=firm["user_id"]
    )
    assert result["updated"] >= 1
    assert result["skipped_no_excerpt"] == 0
    # After recompute, note vs default rates must not ship as case-impact.
    after_fields = {row.get("field") for row in result.get("after") or []}
    assert "stated_interest_rate" not in after_fields

    snaps = store.list_prior_snapshots(firm["firm_id"], matter["id"])
    assert snaps
    rates = [
        f
        for f in (snaps[0].get("facts") or [])
        if f.get("key") == "stated_interest_rate"
    ]
    purposes = {str(f.get("rate_purpose") or "") for f in rates}
    assert "default" in purposes
    assert "note" in purposes

    # Stale rows are excluded from the default findings list.
    assert not [
        f
        for f in store.list_findings(firm["firm_id"], matter["id"])
        if f.get("finding_type") == "fact_conflict"
    ]
    reset_store()


def test_attempts_per_sha_blocks_fourth_retry(tmp_path, monkeypatch):
    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-attempts")
    db = tmp_path / "attempts.sqlite3"
    monkeypatch.setenv("FACT_MEMORY_SQLITE_PATH", str(db))
    monkeypatch.delenv("DATABASE_URL", raising=False)
    reset_store()
    store = FactMemoryStore(database_url=f"sqlite:///{db}")
    firm = store.ensure_firm_for_email("ops@example.com", name="Ops")
    matter = store.create_matter(firm["firm_id"], name="tesst", user_id=firm["user_id"])
    sha = "b" * 64
    for i, status in enumerate(("failed", "failed", "failed"), start=1):
        store.upsert_analysis_job(
            job_id=f"job-{i}",
            firm_id=firm["firm_id"],
            matter_id=matter["id"],
            sha256=sha,
            status=status,
            attempts=i,
            error="local_timeout:ocr_text",
            message="Local stage timed out",
        )
    assert store.max_attempts_for_sha(
        firm_id=firm["firm_id"], matter_id=matter["id"], sha256=sha
    ) == 3
    retryable = store.list_retryable_failed_jobs(
        firm_id=firm["firm_id"], matter_id=matter["id"], max_attempts=3
    )
    assert not any(r.get("sha256") == sha for r in retryable)
    exhausted = store.list_exhausted_jobs(
        firm_id=firm["firm_id"], matter_id=matter["id"], max_attempts=3
    )
    assert any(r.get("sha256") == sha for r in exhausted)
    reset_store()
