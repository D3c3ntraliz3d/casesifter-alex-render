"""A required visual review that did not succeed must not clear the exhibit.

Court-filing skips and an explicit local-only mode still show their real
local result. A finished visual review with no findings can stay low risk.
"""

from __future__ import annotations

import json

import pytest

from app.models import Finding
from app.report.scoring import apply_review_presentation, risk_banner

STATEMENT = """Citibank
Account number 100200
Statement date April 16, 2007
Amount due $40.00
"""

FORBIDDEN = (
    "routine file",
    "no material findings",
    "did not find strong signs of digital alteration",
    "no high-confidence manipulation indicators were found",
)


def _blob(presented: dict) -> str:
    parts = [
        str(presented["executive_summary"]),
        " ".join(presented["recommended_actions"]),
        risk_banner(presented["risk_level"], []),
    ]
    return " ".join(parts).lower()


def test_incomplete_visual_review_is_not_low_risk():
    presented = apply_review_presentation(
        [],
        "Exhibit S_41",
        ai_review="incomplete",
    )
    assert presented["risk_level"] == "incomplete"
    assert presented["risk_level"] != "green"
    assert presented["risk_score"] == 0
    blob = _blob(presented)
    for phrase in FORBIDDEN:
        assert phrase not in blob
    assert "low risk" not in risk_banner(presented["risk_level"], []).lower()
    assert "not cleared" in presented["executive_summary"].lower()
    assert "not a low-risk result" in presented["executive_summary"].lower()
    assert "forged" not in presented["executive_summary"].lower()


def test_completed_review_with_no_findings_stays_low_risk():
    presented = apply_review_presentation(
        [],
        "clean-exhibit.pdf",
        ai_review="complete",
    )
    assert presented["risk_level"] == "green"
    assert "routine file" in presented["executive_summary"].lower()


def test_incomplete_review_keeps_a_real_local_finding():
    finding = Finding(
        id="date-1",
        category="text",
        severity="high",
        title="Statement dates do not reconcile",
        explanation="The coupon due date and the statement due date differ.",
        why_suspicious="The same bill states two different due dates.",
        confidence=100,
        page=5,
        evidence={"analysis_type": "date_inconsistency", "quoted_evidence": "APR. 29, 2007"},
        recommended_action="Compare the two dates on the bill.",
    )
    presented = apply_review_presentation(
        [finding],
        "Exhibit S_41",
        ai_review="incomplete",
    )
    assert presented["risk_level"] in {"yellow", "red"}
    assert presented["risk_level"] != "green"
    summary = presented["executive_summary"].lower()
    assert "routine file" not in summary
    assert "statement dates do not reconcile" in summary
    assert "forged" not in summary


def test_forensic_without_gemini_key_is_not_cleared(monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    monkeypatch.setattr(cl.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", False, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", False, raising=False)

    result = cl.analyze_contextual(
        STATEMENT,
        {"filename": "Exhibit S_41.pdf", "doc_kind": "exhibit"},
        scan_profile="forensic",
    )
    extras = result.extras or {}
    assert extras.get("visual_review_required") is True
    assert get_ai_review_failure() == "no_api_key"
    presented = apply_review_presentation(
        result.findings,
        "Exhibit S_41",
        ai_review="incomplete",
        review_mode=extras.get("review_mode"),
    )
    assert presented["risk_level"] == "incomplete"
    blob = _blob(presented)
    for phrase in FORBIDDEN:
        assert phrase not in blob


def test_multimodal_off_with_key_is_not_cleared(monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", False, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", False, raising=False)

    result = cl.analyze_contextual(
        STATEMENT,
        {"filename": "Exhibit S_41.pdf", "doc_kind": "exhibit"},
        scan_profile="forensic",
    )
    assert get_ai_review_failure() == "multimodal_off"
    assert (result.extras or {}).get("document_analysis") == "skipped_multimodal_off"
    presented = apply_review_presentation([], "Exhibit S_41", ai_review="incomplete")
    assert presented["risk_level"] != "green"
    assert "routine file" not in presented["executive_summary"].lower()


def test_ocr_starved_light_triage_still_runs_visual(tmp_path, monkeypatch):
    """Sparse OCR that forces FULL must call Gemini, not skip via raw triage.bucket."""
    from app.ai_triage import AiTriage, BUCKET_LIGHT
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "sparse.pdf"
    source.write_bytes(b"%PDF-1.4 sparse exhibit")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", False, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(
        "app.ai_triage.triage_document_ai",
        lambda *_a, **_k: AiTriage(
            bucket=BUCKET_LIGHT, reason="light_signal", confidence="high", signal="notice"
        ),
    )
    called = {"n": 0}

    def fake_run(text, source_file, extras):
        called["n"] += 1
        extras["document_analysis_result"] = {
            "count": 0,
            "file_sent": "application/pdf",
            "overall_note": "ok",
        }
        return [], "enabled"

    monkeypatch.setattr(cl, "_run_document_analysis", fake_run)

    result = cl.analyze_contextual(
        "x y z",  # too thin for statement checks; ocr_starved forces FULL
        {"filename": "Exhibit_U.pdf", "doc_kind": "exhibit", "ocr_starved": True},
        source_file=source,
        scan_profile="forensic",
    )
    extras = result.extras or {}
    assert extras.get("ai_bucket_forced") == "ocr_starved"
    assert extras.get("visual_review_required") is True
    assert called["n"] == 1
    assert extras.get("visual_review") == "complete"
    assert get_ai_review_failure() is None


def test_court_filing_skip_is_not_a_gemini_failure(monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    monkeypatch.setattr(cl.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", False, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", False, raising=False)

    result = cl.analyze_contextual(
        STATEMENT,
        {"filename": "motion.pdf", "doc_kind": "motion"},
        scan_profile="court_filing",
    )
    extras = result.extras or {}
    assert extras.get("visual_review_required") is False
    assert extras.get("review_mode") == "court_filing"
    assert get_ai_review_failure() is None
    presented = apply_review_presentation(
        result.findings,
        "motion.pdf",
        ai_review=None,
        review_mode="court_filing",
    )
    assert presented["risk_level"] == "green"


def test_explicit_local_only_is_labeled_and_can_be_low_risk(monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    monkeypatch.setattr(cl.settings, "gemini_api_key", None, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", False, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", True, raising=False)

    result = cl.analyze_contextual(
        STATEMENT,
        {"filename": "Exhibit S_41.pdf", "doc_kind": "exhibit"},
        scan_profile="forensic",
    )
    extras = result.extras or {}
    assert extras.get("visual_review_required") is False
    assert extras.get("review_mode") == "local_only"
    assert get_ai_review_failure() is None

    reset_ai_review_state()
    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)

    def _should_not_call(*_a, **_k):
        raise AssertionError("local-only review must not call Gemini")

    monkeypatch.setattr(cl, "_post_gemini", _should_not_call)
    result_with_key = cl.analyze_contextual(
        STATEMENT,
        {"filename": "Exhibit S_41.pdf", "doc_kind": "exhibit"},
        scan_profile="forensic",
    )
    assert (result_with_key.extras or {}).get("review_mode") == "local_only"
    assert get_ai_review_failure() is None

    presented = apply_review_presentation(
        result.findings,
        "Exhibit S_41",
        ai_review="local_only",
        review_mode="local_only",
    )
    assert presented["risk_level"] == "green"
    summary = presented["executive_summary"].lower()
    assert "local-only" in summary
    assert "routine file" not in summary
    assert "did not find strong signs of digital alteration" not in summary


def test_empty_gemini_response_is_not_a_clearance():
    from app.engines.contextual_logic import _parse_json_payload
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    with pytest.raises(RuntimeError, match="no candidates"):
        _parse_json_payload({})
    assert get_ai_review_failure() == "empty_response"
    presented = apply_review_presentation([], "Exhibit S_41", ai_review="incomplete")
    assert presented["risk_level"] == "incomplete"
    assert "routine file" not in presented["executive_summary"].lower()


def test_visual_pass_retries_once_then_succeeds(tmp_path, monkeypatch):
    """First visual failure must not Incomplete when the second attempt succeeds."""
    from app.ai_triage import AiTriage, BUCKET_FULL
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, note_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "Exhibit_retry.pdf"
    source.write_bytes(b"%PDF-1.4 retry exhibit")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", False, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(
        "app.ai_triage.triage_document_ai",
        lambda *_a, **_k: AiTriage(
            bucket=BUCKET_FULL, reason="full", confidence="high", signal="statement"
        ),
    )
    calls = {"n": 0}

    def fail_then_ok(text, source_file, extras):
        calls["n"] += 1
        if calls["n"] == 1:
            note_ai_review_failure("timeout")
            extras["document_analysis_error"] = "timeout"
            return [], "error"
        extras["document_analysis_result"] = {
            "count": 0,
            "file_sent": "image/jpeg",
            "overall_note": "ok",
        }
        return [], "enabled"

    monkeypatch.setattr(cl, "_run_document_analysis", fail_then_ok)
    # Keep tier-2 claim calls from overwriting the visual retry success state.
    monkeypatch.setattr(cl, "_verify_claims_parallel", lambda *_a, **_k: [])
    monkeypatch.setattr(cl, "build_claims", lambda *_a, **_k: [])

    result = cl.analyze_contextual(
        STATEMENT,
        {"filename": "Exhibit_retry.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    extras = result.extras or {}
    assert calls["n"] == 2
    assert extras.get("visual_review_attempt") == 2
    assert extras.get("visual_review") == "complete"
    assert get_ai_review_failure() is None
    presented = apply_review_presentation(
        result.findings,
        "Exhibit_retry.pdf",
        ai_review="complete",
    )
    assert presented["risk_level"] == "green"


def test_visual_pass_double_fail_stays_incomplete(tmp_path, monkeypatch):
    """Two failed visual attempts still fail closed as Incomplete — no third try."""
    from app.ai_triage import AiTriage, BUCKET_FULL
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, note_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "Exhibit_fail.pdf"
    source.write_bytes(b"%PDF-1.4 fail exhibit")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "local_only_review", False, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(
        "app.ai_triage.triage_document_ai",
        lambda *_a, **_k: AiTriage(
            bucket=BUCKET_FULL, reason="full", confidence="high", signal="statement"
        ),
    )
    calls = {"n": 0}

    def always_fail(text, source_file, extras):
        calls["n"] += 1
        note_ai_review_failure("error")
        extras["document_analysis_error"] = f"boom-{calls['n']}"
        return [], "error"

    monkeypatch.setattr(cl, "_run_document_analysis", always_fail)
    monkeypatch.setattr(cl, "_verify_claims_parallel", lambda *_a, **_k: [])
    monkeypatch.setattr(cl, "build_claims", lambda *_a, **_k: [])

    result = cl.analyze_contextual(
        STATEMENT,
        {"filename": "Exhibit_fail.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    extras = result.extras or {}
    assert calls["n"] == 2
    assert extras.get("visual_review_attempts") == 2
    assert extras.get("visual_review") == "incomplete"
    assert get_ai_review_failure() == "error"
    presented = apply_review_presentation(
        result.findings,
        "Exhibit_fail.pdf",
        ai_review="incomplete",
    )
    assert presented["risk_level"] == "incomplete"


def test_pipeline_forensic_without_key_does_not_clear(tmp_path, monkeypatch):
    from app.config import Settings
    import app.config as config_mod
    import app.storage as storage
    from app.models import ModuleResult, Report
    from app.pipeline import run_gemini_stage

    monkeypatch.setenv("FACT_MEMORY_MASTER_KEY", "unit-test-master-key-fail-closed")
    monkeypatch.setenv("DATA_DIR", str(tmp_path / "data"))
    (tmp_path / "data").mkdir()
    settings = Settings()
    settings.gemini_api_key = None
    settings.contextual_multimodal = False
    settings.local_only_review = False
    monkeypatch.setattr(config_mod, "settings", settings)
    monkeypatch.setattr(storage, "settings", settings)
    monkeypatch.setattr("app.pipeline.settings", settings)
    monkeypatch.setattr("app.engines.contextual_logic.settings", settings)
    monkeypatch.setattr("app.ai_fact_extract.settings", settings, raising=False)

    job_id = "a" * 32
    storage.create_job(job_id, "Exhibit_S_41.pdf")
    stage = {
        "submitted_by": "test",
        "case_id": None,
        "document_id": None,
        "display_name": "Exhibit S_41",
        "doc_kind": "exhibit",
        "prior_snapshots": [],
        "profile": "forensic",
        "court_filing": False,
        "file_type": "application/pdf",
        "digest": "b" * 64,
        "size": 12,
        "origin_for_profile": "scan",
        "source_name": "Exhibit_S_41.pdf",
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
        "full_text": STATEMENT,
        "page_offsets": [{"page": 1, "start": 0, "end": len(STATEMENT)}],
        "local_findings": [],
        "local_facts": [],
        "amounts": [],
        "fingerprint": "fp",
        "hashes": [],
        "local_s": 0.1,
    }
    (storage.job_dir(job_id) / "local_stage.json").write_text(json.dumps(stage), encoding="utf-8")
    source = storage.job_dir(job_id) / "Exhibit_S_41.pdf"
    source.write_bytes(b"%PDF-1.4 fake")

    monkeypatch.setattr("app.pipeline.review_print_scan_ela", lambda findings, *_a, **_k: findings)
    monkeypatch.setattr("app.pipeline.triage_findings", lambda findings, *_a, **_k: findings)
    monkeypatch.setattr(
        "app.pipeline.apply_case_impact_judge",
        lambda findings: (findings, {"kept": len(findings)}),
    )
    monkeypatch.setattr("app.pipeline.render_pdf", lambda report: None)
    monkeypatch.setattr("app.pipeline.emit_scan_completed", lambda **_k: None)
    monkeypatch.setattr("app.pipeline.emit_scan_outcomes", lambda **_k: None)

    report = run_gemini_stage(job_id, source)
    assert isinstance(report, Report)
    assert report.ai_review == "incomplete"
    assert report.ai_review_reason == "no_api_key"
    assert report.comparison.get("visual_review_required") is True
    assert report.risk_level == "incomplete"
    assert report.risk_level != "green"
    summary = report.executive_summary.lower()
    for phrase in FORBIDDEN:
        assert phrase not in summary
    assert "not a low-risk result" in summary
    html = (storage.job_dir(job_id) / "report.html").read_text(encoding="utf-8")
    assert "No material findings." not in html
    assert "routine file" not in html.lower()
    progress = storage.load_progress(job_id)
    assert progress is not None
    assert "not cleared" in (progress.message or "").lower()
