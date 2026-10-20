"""Document row must drop the previous Complete / risk while a rescan is open."""

from app.scan_row import overlay_inflight_scans


def _doc():
    return {
        "id": "d1",
        "sha256": "ABC",
        "status": "complete",
        "risk_level": "red",
        "risk_score": 90,
        "ai_review": "complete",
        "scan_job_id": "old",
        "filed_date": "2024-03-01",
        "scan_modules": [
            {"name": "metadata", "status": "ok"},
            {"name": "image_forensics", "status": "error"},
        ],
    }


def test_open_rescan_clears_complete_and_keeps_filed_date():
    out = overlay_inflight_scans(
        [_doc()],
        {"abc": {"job_id": "new", "status": "running", "stage": "structural"}},
    )[0]
    assert out["status"] == "scanning"
    assert out["risk_level"] is None
    assert out["risk_score"] is None
    assert out["ai_review"] is None
    assert out["scan_in_progress"] is True
    assert out["stage"] == "structural"
    assert out["scan_job_id"] == "new"
    assert out["filed_date"] == "2024-03-01"
    # Prior modules stay for which dots to show — colors stay yellow in the UI.
    assert out["scan_modules"][1]["status"] == "error"
    assert out.get("modules_from_job") is not True


def test_early_persist_same_job_id_still_shows_scanning():
    """last_job_id may already equal the in-flight job after local_done persist."""
    doc = _doc()
    doc["scan_job_id"] = "same"
    doc["scan_modules"] = [
        {"name": "metadata", "status": "ok"},
        {"name": "image_forensics", "status": "ok"},
    ]
    out = overlay_inflight_scans(
        [doc],
        {"abc": {"job_id": "same", "status": "running", "stage": "contextual"}},
    )[0]
    assert out["scan_in_progress"] is True
    assert out["status"] == "scanning"
    assert out["risk_level"] is None
    assert out["stage"] == "contextual"


def test_same_job_complete_keeps_the_finished_result():
    doc = _doc()
    doc["scan_job_id"] = "same"
    out = overlay_inflight_scans(
        [doc],
        {"abc": {"job_id": "same", "status": "complete", "stage": "generating_report"}},
    )[0]
    assert out["status"] == "complete"
    assert out["risk_level"] == "red"
    assert out["filed_date"] == "2024-03-01"


def test_failed_newer_job_does_not_restore_high_risk():
    out = overlay_inflight_scans(
        [_doc()],
        {"abc": {"job_id": "new", "status": "failed", "stage": "image_forensics", "error": "boom"}},
    )[0]
    assert out["status"] == "failed"
    assert out["scan_failed_open"] is True
    assert out["risk_level"] is None
    assert out["last_error"] == "boom"
    assert out["filed_date"] == "2024-03-01"


def test_interrupted_job_status_is_interrupted_not_failed():
    out = overlay_inflight_scans(
        [_doc()],
        {
            "abc": {
                "job_id": "new",
                "status": "interrupted",
                "stage": "interrupted",
                "error": "interrupted_by_restart",
            }
        },
    )[0]
    assert out["status"] == "interrupted"
    assert out["scan_failed_open"] is True
    assert out["risk_level"] is None
    assert out["last_error"] == "interrupted_by_restart"


def test_complete_newer_job_before_report_attach_stays_in_progress():
    """Job finished but last_job_id still points at the old report — keep Scanning."""
    out = overlay_inflight_scans(
        [_doc()],
        {"abc": {"job_id": "new", "status": "complete", "stage": "generating_report"}},
    )[0]
    assert out["status"] == "scanning"
    assert out["scan_in_progress"] is True
    assert out["risk_level"] is None
    assert out["scan_job_id"] == "new"
    assert out["filed_date"] == "2024-03-01"


def test_unattached_queued_job_appears_as_provisional_row():
    """Hosted upload creates a job before Document persist — list must not drop it."""
    out = overlay_inflight_scans(
        [],
        {
            "deadbeef" * 8: {
                "job_id": "job1",
                "status": "queued",
                "original_filename": "01 - AFFIRMATION.pdf",
                "display_name": "Affirmation",
                "doc_kind": "affirmation",
            }
        },
    )
    assert len(out) == 1
    row = out[0]
    assert row["id"] == "pending:job1"
    assert row["pending_upload"] is True
    assert row["provisional"] is True
    assert row["status"] == "queued"
    assert row["scan_in_progress"] is True
    assert row["display_name"] == "Affirmation"
    assert row["original_filename"] == "01 - AFFIRMATION.pdf"
    assert row["doc_kind"] == "affirmation"


def test_unattached_job_does_not_duplicate_existing_doc():
    docs = [{"id": "d1", "sha256": "abc", "status": "complete", "scan_job_id": "old"}]
    out = overlay_inflight_scans(
        docs,
        {"abc": {"job_id": "new", "status": "running", "stage": "ocr"}},
    )
    assert len(out) == 1
    assert out[0]["id"] == "d1"
    assert out[0]["status"] == "scanning"


def test_unattached_failed_job_stays_visible():
    out = overlay_inflight_scans(
        [],
        {
            "cafe" * 16: {
                "job_id": "fail1",
                "status": "failed",
                "error": "timeout",
                "filename": "exhibit.pdf",
            }
        },
    )
    assert len(out) == 1
    assert out[0]["status"] == "failed"
    assert out[0]["scan_failed_open"] is True
    assert out[0]["last_error"] == "timeout"
    assert out[0]["original_filename"] == "exhibit.pdf"


def test_vault_filed_date_persists(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from app import cases_db

    cases_db._INITIALIZED = False
    cases_db.ensure_schema()
    case = cases_db.create_case(caption="Filed date")
    doc = cases_db.add_document(
        case["id"],
        original_filename="a.pdf",
        display_name="A",
        doc_kind="exhibit",
        stored_path=str(tmp_path / "a.pdf"),
    )
    saved = cases_db.update_document(doc["id"], filed_date="2020-05-04", status="complete", risk_level="red")
    assert saved["filed_date"] == "2020-05-04"
    rescanning = cases_db.update_document(doc["id"], status="queued", risk_level=None, risk_score=None)
    assert rescanning["status"] == "queued"
    assert rescanning["risk_level"] is None
    assert rescanning["filed_date"] == "2020-05-04"
    again = cases_db.get_document(doc["id"])
    assert again["filed_date"] == "2020-05-04"
