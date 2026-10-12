"""Priors with empty facts + terminal extraction status must never re-call Gemini."""

from __future__ import annotations

from app.case_consistency import compare_to_case, fact_conflicts_from_packet
from app.extraction_status import (
    STATUS_EXTRACTED_OK,
    STATUS_FAILED,
    STATUS_SKIPPED_BOILERPLATE,
    derive_extraction_status,
    needs_extraction_backfill,
)


def test_derive_status_ok_even_with_zero_facts():
    status, err = derive_extraction_status(
        {"calls": 1, "facts_added": 0}, enable_ai=True
    )
    assert status == STATUS_EXTRACTED_OK
    assert err is None


def test_derive_status_boilerplate_and_failed():
    status, _ = derive_extraction_status(
        {"skipped": "boilerplate_filing", "calls": 0}, enable_ai=True
    )
    assert status == STATUS_SKIPPED_BOILERPLATE
    status, err = derive_extraction_status(
        {"skipped": "gemini_error", "error": "503"}, enable_ai=True
    )
    assert status == STATUS_FAILED
    assert "503" in (err or "")


def test_needs_backfill_rules():
    assert not needs_extraction_backfill(
        {"extraction_status": STATUS_EXTRACTED_OK, "text_excerpt": "x", "facts": []}
    )
    assert not needs_extraction_backfill(
        {
            "extraction_status": STATUS_SKIPPED_BOILERPLATE,
            "text_excerpt": "x",
            "facts": [],
        }
    )
    assert needs_extraction_backfill(
        {
            "extraction_status": STATUS_FAILED,
            "extraction_retry_done": False,
            "text_excerpt": "body",
            "facts": [],
        }
    )
    assert not needs_extraction_backfill(
        {
            "extraction_status": STATUS_FAILED,
            "extraction_retry_done": True,
            "text_excerpt": "body",
            "facts": [],
        }
    )
    assert needs_extraction_backfill(
        {"text_excerpt": "legacy body", "facts": [], "document_id": "d1"}
    )


def test_compare_to_case_never_extracts_empty_priors(monkeypatch):
    from app import case_consistency as cc

    calls = {"n": 0}

    def boom(*_a, **_k):
        calls["n"] += 1
        raise AssertionError("extract_facts must not run for priors")

    monkeypatch.setattr(cc, "extract_facts", boom, raising=False)
    # Also patch module-level import path if rebound later.
    import app.fact_extract as fe

    monkeypatch.setattr(fe, "extract_facts", boom)

    findings, matrix = compare_to_case(
        new_document_id="new",
        new_display_name="New Affidavit",
        new_entities={"organizations": [], "locations": []},
        new_amounts=[],
        new_fingerprint="fp-new",
        prior_snapshots=[
            {
                "document_id": "prior1",
                "display_name": "Prior Utility",
                "facts": [],
                "text_excerpt": "Account Statement Amount Due $10 Statement Date April 1 2007",
                "extraction_status": STATUS_EXTRACTED_OK,
                "entities": {},
            },
            {
                "document_id": "prior2",
                "display_name": "Prior Bill",
                "facts": [],
                "text_excerpt": "NATIONAL GRID Account Statement",
                "extraction_status": STATUS_SKIPPED_BOILERPLATE,
                "entities": {},
            },
        ],
        new_facts=[
            {
                "key": "principal_balance",
                "value": 100.0,
                "display": "100.00",
                "kind": "money",
            }
        ],
        new_text="Affidavit principal balance $100",
    )
    assert calls["n"] == 0
    assert isinstance(findings, list)
    assert isinstance(matrix, list)


def test_fact_conflicts_from_packet_uses_stored_facts_only(monkeypatch):
    import app.fact_extract as fe

    def boom(*_a, **_k):
        raise AssertionError("packet rebuild must not extract")

    monkeypatch.setattr(fe, "extract_facts", boom)
    hits = fact_conflicts_from_packet(
        [
            {
                "document_id": "a",
                "display_name": "A",
                "text_excerpt": "Amount Due $99",
                "facts": [],
            },
            {
                "document_id": "b",
                "display_name": "B",
                "text_excerpt": "Amount Due $1",
                "facts": [
                    {
                        "key": "amount_due",
                        "value": 1.0,
                        "display": "1.00",
                        "kind": "money",
                    }
                ],
            },
        ]
    )
    # Empty-facts doc skipped; no extract called.
    assert isinstance(hits, list)


def test_save_text_excerpt_does_not_extract(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import cases_db
    import app.fact_extract as fe

    def boom(*_a, **_k):
        raise AssertionError("save_text_excerpt must not extract_facts")

    monkeypatch.setattr(fe, "extract_facts", boom)
    cases_db._INITIALIZED = False
    cases_db.ensure_schema()
    case = cases_db.create_case(caption="t")
    doc = cases_db.add_document(
        case["id"],
        original_filename="a.pdf",
        display_name="A",
        doc_kind="exhibit",
        stored_path=str(tmp_path / "a.pdf"),
    )
    (tmp_path / "a.pdf").write_bytes(b"%PDF-1.4")
    cases_db.save_text_excerpt(doc["id"], case["id"], "hello world excerpt for chat")
    snap = cases_db.get_scan_snapshot(doc["id"])
    assert snap is not None
    assert snap["text_excerpt"].startswith("hello")
    assert snap["facts"] == []


def test_backfill_enqueue_dedupes(monkeypatch):
    from app import fact_extract_backfill as bf

    monkeypatch.setattr(bf, "_pending", bf.deque())
    monkeypatch.setattr(bf, "_queued_ids", set())
    # Don't start a real worker loop race — just queue.
    monkeypatch.setattr(bf, "ensure_extract_backfill_worker", lambda: None)

    snaps = [
        {
            "document_id": "d1",
            "text_excerpt": "Account statement",
            "extraction_status": STATUS_FAILED,
            "extraction_retry_done": False,
            "facts": [],
        },
        {
            "document_id": "d1",
            "text_excerpt": "Account statement",
            "extraction_status": STATUS_FAILED,
            "extraction_retry_done": False,
            "facts": [],
        },
        {
            "document_id": "d2",
            "text_excerpt": "ok",
            "extraction_status": STATUS_EXTRACTED_OK,
            "facts": [],
        },
    ]
    n1 = bf.enqueue_extraction_backfill(snaps, scope="vault", case_id="c1")
    n2 = bf.enqueue_extraction_backfill(snaps, scope="vault", case_id="c1")
    assert n1 == 1
    assert n2 == 0
