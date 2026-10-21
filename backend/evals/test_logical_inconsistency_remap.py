"""logical_inconsistency must remap to shippable types for CIGNA-class evidence."""

from __future__ import annotations

from pathlib import Path

from app.engines.contextual_logic import (
    _finding_from_analysis,
    _findings_from_analysis_result,
    _remap_logical_inconsistency,
)
from app.finding_policy import is_critical_math_error, is_material_finding


def _cigna_period_ytd_item() -> dict:
    return {
        "analysis_type": "logical_inconsistency",
        "title": "CIGNA period vs YTD performance contradiction on page 4",
        "severity": "high",
        "confidence": 88,
        "page": 4,
        "observation": (
            "The CIGNA retirement statement shows mutually contradictory period and "
            "year-to-date performance calculations spanning the same calendar period "
            "starting January 1, 2009."
        ),
        "why_suspicious": (
            "Period and YTD returns cannot both be correct for the same January–February "
            "2009 window on an authentic issuer statement."
        ),
        "quoted_evidence": "Period return 1.2% YTD return -0.4% Jan. 1, 2009 - Feb. 28, 2009",
    }


def _cigna_template_date_item() -> dict:
    return {
        "analysis_type": "logical_inconsistency",
        "title": "CIGNA 2004 production date on 2009 statement",
        "severity": "high",
        "confidence": 86,
        "page": 4,
        "observation": (
            "CIGNA statement on page 4 contains a 2004 print production date for a "
            "purported 2009 statement period."
        ),
        "why_suspicious": (
            "A production tracking timestamp years before the statement period is leftover "
            "template chrome, not an authentic contemporaneous print."
        ),
        "quoted_evidence": "Production tracking 05/12/2004 Statement period Jan. 1, 2009",
    }


def _advocacy_party_name_item() -> dict:
    return {
        "analysis_type": "logical_inconsistency",
        "title": "Party named inconsistently",
        "severity": "medium",
        "confidence": 80,
        "page": 1,
        "observation": "The caption lists one spelling of the defendant and the body another.",
        "why_suspicious": "Inconsistent party naming across the pleading.",
        "quoted_evidence": "Defendant Roberts / Defendant Robert",
    }


def test_remap_cigna_period_ytd_to_math_error():
    assert _remap_logical_inconsistency(_cigna_period_ytd_item()) == "math_error"


def test_remap_cigna_template_date_to_date_inconsistency():
    assert _remap_logical_inconsistency(_cigna_template_date_item()) == "date_inconsistency"


def test_remap_advocacy_party_name_stays_unmapped():
    assert _remap_logical_inconsistency(_advocacy_party_name_item()) is None


def test_cigna_period_ytd_ships_as_math_error():
    out = _finding_from_analysis(
        _cigna_period_ytd_item(),
        0,
        {"model": "gemini-test", "file_sent": "image/jpeg"},
        text="CIGNA Retirement Statement Jan. 1, 2009",
    )
    assert out is not None
    assert out.evidence["analysis_type"] == "math_error"
    assert out.evidence.get("remapped_from") == "logical_inconsistency"
    assert out.evidence.get("model_analysis_type") == "logical_inconsistency"
    assert "Arithmetic" in out.title or "math" in out.title.lower() or "CIGNA" in out.title
    assert is_material_finding(out)
    assert is_critical_math_error(out)


def test_cigna_template_date_ships_as_date_inconsistency():
    out = _finding_from_analysis(
        _cigna_template_date_item(),
        0,
        {"model": "gemini-test", "file_sent": "image/jpeg"},
        text="CIGNA Retirement Statement Jan. 1, 2009",
    )
    assert out is not None
    assert out.evidence["analysis_type"] == "date_inconsistency"
    assert out.evidence.get("remapped_from") == "logical_inconsistency"
    assert is_material_finding(out)


def test_advocacy_logical_inconsistency_still_dropped():
    out = _finding_from_analysis(
        _advocacy_party_name_item(),
        0,
        {"model": "gemini-test"},
        text="SUMMONS AND COMPLAINT",
    )
    assert out is None


def test_spelling_anomaly_still_dropped():
    out = _finding_from_analysis(
        {
            "analysis_type": "spelling_anomaly",
            "title": "Misspelled city",
            "severity": "medium",
            "confidence": 90,
            "page": 1,
            "observation": "City name is misspelled.",
            "why_suspicious": "Issuer would not print this.",
            "quoted_evidence": "Brooklyn spelled Broklyn",
        },
        0,
        {"model": "gemini-test"},
    )
    assert out is None


def test_raw_findings_persisted_before_filter(caplog):
    result = {
        "model": "gemini-test",
        "file_sent": "image/jpeg",
        "overall_note": (
            "National Grid on page 5 has a past next meter reading; "
            "CIGNA statement on page 4 has contradictory period and YTD performance."
        ),
        "findings": [
            {
                "analysis_type": "date_inconsistency",
                "title": "National Grid next meter reading predates bill date",
                "severity": "high",
                "confidence": 90,
                "page": 5,
                "observation": "Next Meter Reading Dec 06 '08 predates Bill Date Jan 07 '09.",
                "why_suspicious": "Future scheduled reading cannot predate the bill.",
                "quoted_evidence": "Next Meter Reading Dec 06 '08 Bill Date Jan 07 '09",
            },
            _cigna_period_ytd_item(),
            _advocacy_party_name_item(),
        ],
    }
    extras: dict = {}
    with caplog.at_level("WARNING"):
        findings, status = _findings_from_analysis_result(
            result,
            text="CIGNA Retirement Statement. National Grid bill.",
            source_file=Path("Place_of_Residence.pdf"),
            extras=extras,
        )
    assert status == "enabled"
    dar = extras["document_analysis_result"]
    assert dar["count"] == 3
    assert dar["shipped_count"] == 2
    assert dar["dropped_count"] == 1
    assert len(dar["raw_findings"]) == 3
    assert {r["analysis_type"] for r in dar["raw_findings"]} == {
        "date_inconsistency",
        "logical_inconsistency",
    }
    dropped = dar["dropped_findings"]
    assert len(dropped) == 1
    assert dropped[0]["drop_reason"] == "logical_inconsistency_unmapped"
    shipped_types = {f.evidence.get("analysis_type") for f in findings}
    assert shipped_types == {"date_inconsistency", "math_error"}
    # Completeness: both issuers appear in shipped titles after remap — no gap warning required.
    assert not any("completeness gap" in r.message for r in caplog.records)


def test_completeness_logs_when_note_issuer_missing(caplog):
    result = {
        "model": "gemini-test",
        "file_sent": "image/jpeg",
        "overall_note": (
            "National Grid statement on page 5 lists a past next meter reading, "
            "and the CIGNA statement on page 4 contains contradictory period and YTD math."
        ),
        "findings": [
            {
                "analysis_type": "date_inconsistency",
                "title": "National Grid next meter reading predates bill date",
                "severity": "high",
                "confidence": 90,
                "page": 5,
                "observation": "Next Meter Reading Dec 06 '08 predates Bill Date Jan 07 '09.",
                "why_suspicious": "Future scheduled reading cannot predate the bill.",
                "quoted_evidence": "Next Meter Reading Dec 06 '08 Bill Date Jan 07 '09",
            },
            # Model under-emitted CIGNA as a finding entirely (only overall_note).
        ],
    }
    extras: dict = {}
    with caplog.at_level("WARNING"):
        findings, status = _findings_from_analysis_result(
            result,
            text="National Grid bill.",
            source_file=Path("Place_of_Residence.pdf"),
            extras=extras,
        )
    assert status == "enabled"
    assert len(findings) == 1
    assert any(
        "completeness gap" in r.message and "CIGNA" in r.message for r in caplog.records
    )
