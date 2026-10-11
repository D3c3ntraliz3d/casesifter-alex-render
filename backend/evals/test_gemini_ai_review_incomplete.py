"""P1-10: failed Gemini calls must surface ai_review incomplete (never silent complete)."""

from __future__ import annotations

import io
import json
import urllib.error

import pytest


class _FakeHTTPError(urllib.error.HTTPError):
    def __init__(self, code: int, body: bytes = b'{"error":"quota"}'):
        # HTTPError.__init__(url, code, msg, hdrs, fp)
        super().__init__(
            "https://generativelanguage.googleapis.com/fake",
            code,
            f"HTTP {code}",
            hdrs={},
            fp=io.BytesIO(body),
        )


def test_post_gemini_429_then_402_marks_billing(monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_cancel import set_current_job
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    set_current_job("aabbccdd11223344")

    calls = {"n": 0}

    def fake_urlopen(req, timeout=None):
        calls["n"] += 1
        if calls["n"] <= 2:
            raise _FakeHTTPError(429)
        raise _FakeHTTPError(402)

    monkeypatch.setattr(cl.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(cl.time, "sleep", lambda _s: None)
    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "gemini_timeout_s", 5, raising=False)

    from app.gemini_tasks import TASK_A_EXTRACT

    with pytest.raises(RuntimeError, match="HTTP 402"):
        cl._post_gemini(
            {"contents": [{"role": "user", "parts": [{"text": "x"}]}]},
            task=TASK_A_EXTRACT,
            step="test",
            chunk_id="t",
        )

    assert get_ai_review_failure() == "billing"
    # Primary: 429, 429, then 402 (non-retryable) — stops before backup.
    assert calls["n"] == 3
    set_current_job(None)


def test_pipeline_records_incomplete_ai_review_not_clean_complete(tmp_path, monkeypatch):
    """Gemini ultimate failure must land on the report (no silent complete)."""
    from app.config import Settings
    import app.config as config_mod
    import app.storage as storage
    from app.gemini_cancel import set_current_job
    from app.gemini_review_state import note_ai_review_failure, reset_ai_review_state
    from app.models import ModuleResult, Report
    from app.pipeline import run_gemini_stage

    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-ai-review")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    settings = Settings()
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(storage, "settings", settings)
    monkeypatch.setattr("app.pipeline.settings", settings)

    job_id = "f" * 32
    storage.create_job(job_id, "exhibit.pdf")
    stage = {
        "submitted_by": "test",
        "case_id": None,
        "document_id": None,
        "display_name": "exhibit.pdf",
        "doc_kind": "exhibit",
        "prior_snapshots": [],
        "profile": "forensic",
        "court_filing": False,
        "file_type": "application/pdf",
        "digest": "a" * 64,
        "size": 12,
        "origin_for_profile": "scan",
        "source_name": "exhibit.pdf",
        "metadata": ModuleResult(
            name="metadata", status="ok", summary="ok", findings=[], extras={"timeline": [], "metadata": {"document": {}}}
        ).model_dump(mode="json"),
        "pdf": ModuleResult(name="pdf", status="ok", summary="ok", findings=[], extras={}).model_dump(mode="json"),
        "structural": ModuleResult(
            name="structural", status="ok", summary="ok", findings=[], extras={"dimension_models": [], "page_dimensions": []}
        ).model_dump(mode="json"),
        "images": ModuleResult(
            name="images", status="ok", summary="ok", findings=[], extras={"artifacts": []}
        ).model_dump(mode="json"),
        "ocr": ModuleResult(
            name="ocr", status="ok", summary="ok", findings=[], extras={"fonts": [], "font_models": []}
        ).model_dump(mode="json"),
        "full_text": "Sample exhibit text with an amount of $100.",
        "page_offsets": [{"page": 1, "start": 0, "end": 40}],
        "local_findings": [],
        "local_facts": [],
        "amounts": [],
        "fingerprint": "fp",
        "hashes": [],
        "local_s": 1.0,
    }
    (storage.job_dir(job_id) / "local_stage.json").write_text(json.dumps(stage), encoding="utf-8")
    source = storage.job_dir(job_id) / "exhibit.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    reset_ai_review_state()
    set_current_job(job_id)

    def boom(*_a, **_k):
        note_ai_review_failure("billing")
        raise RuntimeError("Gemini returned HTTP 402: billing")

    # Fail every Gemini entry point used by the stage; local modules still assemble a report.
    monkeypatch.setattr("app.engines.contextual_logic._post_gemini", boom)
    monkeypatch.setattr("app.ai_fact_extract._post_gemini", boom, raising=False)

    # Avoid network / heavy side effects in peripheral helpers.
    monkeypatch.setattr("app.pipeline.review_print_scan_ela", lambda findings, *_a, **_k: findings)
    monkeypatch.setattr("app.pipeline.triage_findings", lambda findings, *_a, **_k: findings)
    monkeypatch.setattr(
        "app.pipeline.apply_case_impact_judge",
        lambda findings: (findings, {"kept": len(findings)}),
    )
    monkeypatch.setattr("app.pipeline.render_html", lambda report: "<html></html>")
    monkeypatch.setattr("app.pipeline.render_pdf", lambda report: None)
    monkeypatch.setattr("app.pipeline.emit_scan_completed", lambda **_k: None)
    monkeypatch.setattr("app.pipeline.emit_scan_outcomes", lambda **_k: None)

    report = run_gemini_stage(job_id, source)
    set_current_job(None)

    assert isinstance(report, Report)
    assert report.ai_review == "incomplete"
    assert report.ai_review_reason == "billing"
    assert report.comparison.get("ai_review") == "incomplete"
    # Must not look like a clean complete AI review.
    assert report.ai_review != "complete"
    assert not (storage.job_dir(job_id) / "retain_local_stage.flag").exists()
    progress = storage.load_progress(job_id)
    assert progress is not None
    assert progress.status == "complete"
    assert "incomplete" in (progress.message or "").lower()


def test_forensic_file_not_sent_marks_incomplete(tmp_path, monkeypatch):
    """Skipped Gemini upload on forensic exhibits → ai_review incomplete / file_not_sent."""
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "exhibit.pdf"
    source.write_bytes(b"%PDF-1.4 fake exhibit")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(
        cl,
        "_encode_source_file",
        lambda _p: (None, None, "File is over the inline upload limit; analyzed text only."),
    )

    def fake_analyze(text, file_base64, mime_type, extras=None):
        assert file_base64 is None
        return {
            "findings": [],
            "overall_note": "text only",
            "file_sent": None,
            "model": "gemini-test",
        }

    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr(
        "app.gemini_inflight.acquire",
        lambda *_a, **_k: True,
    )
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "Sample bank statement text with $100.",
        {"filename": "exhibit.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    extras = result.extras or {}
    assert extras.get("document_analysis_result", {}).get("file_sent") in (None, False, "")
    assert get_ai_review_failure() == "file_not_sent"

    # Court filings skip multimodal upload and must not be marked incomplete for this reason.
    reset_ai_review_state()
    result2 = cl.analyze_contextual(
        "SUMMONS AND COMPLAINT",
        {"filename": "summons.pdf", "doc_kind": "pleading"},
        source_file=source,
        scan_profile="court_filing",
    )
    assert (result2.extras or {}).get("document_analysis") == "skipped_court_filing"
    assert get_ai_review_failure() is None


def test_reason_for_http_status_mapping():
    from app.gemini_review_state import reason_for_http_status

    assert reason_for_http_status(429) == "rate_limited"
    assert reason_for_http_status(402) == "billing"
    assert reason_for_http_status(403) == "billing"
    assert reason_for_http_status(500) == "error"
