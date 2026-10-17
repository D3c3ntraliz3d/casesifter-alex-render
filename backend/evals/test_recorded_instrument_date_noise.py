"""Recording vs execution/assignment lag on deeds/mortgages must not interrupt."""

from __future__ import annotations

from pathlib import Path

from app.engines.contextual_logic import _finding_from_analysis
from app.finding_policy import (
    drop_noise_findings,
    is_header_or_arrears_noise,
    is_material_finding,
    is_recorded_instrument_date_noise,
)
from app.models import Finding


MORTGAGE = (
    "This Mortgage, to be recorded in the Office of the City Register, "
    "CRFN 2006000123456, Block 1 Lot 2. Mortgage dated 03/01/2006. "
    "Recorded May 15, 2006."
)


def _lag_finding() -> Finding:
    return Finding(
        id="ctx-ai-mortgage-lag",
        category="contextual",
        severity="high",
        title="Mortgage date does not match recording date",
        explanation="Mortgage dated 03/01/2006 but recorded 05/15/2006.",
        why_suspicious="Execution and recording dates do not match.",
        confidence=80,
        evidence={
            "analysis_type": "date_inconsistency",
            "quoted_evidence": "Mortgage dated 03/01/2006. Recorded May 15, 2006.",
        },
    )


def test_recording_lag_is_date_noise():
    item = {
        "analysis_type": "date_inconsistency",
        "title": "Mortgage date does not match recording date",
        "observation": "Mortgage dated 03/01/2006 but recorded 05/15/2006.",
        "quoted_evidence": "Mortgage dated 03/01/2006. Recorded May 15, 2006.",
    }
    assert is_recorded_instrument_date_noise(item, text=MORTGAGE, source="Mortgage")
    assert is_header_or_arrears_noise(item)


def test_assignment_recording_lag_is_date_noise():
    item = {
        "analysis_type": "date_inconsistency",
        "title": "Assignment date vs recording date discrepancy",
        "observation": "Assignment dated March 1, 2015; recorded May 12, 2015.",
        "quoted_evidence": "Assignment of Mortgage dated March 1, 2015.",
    }
    assert is_recorded_instrument_date_noise(
        item, text="", source="Assignment of Mortgage — CRFN exhibit"
    )


def test_recording_lag_dropped_and_not_material():
    finding = _lag_finding()
    assert drop_noise_findings([finding]) == []
    assert not is_material_finding(finding)


def test_statement_due_date_still_counts():
    item = {
        "analysis_type": "date_inconsistency",
        "title": "Statement due date precedes its own period",
        "observation": "Due date years after statement date.",
        "quoted_evidence": "Statement date April 2007. Next due date December 2011.",
    }
    assert not is_recorded_instrument_date_noise(
        item, text="Account number 1. Amount due $10. Statement date April 2007.", source="Bill"
    )


def test_ai_path_drops_recording_lag():
    result = {"model": "test", "file_sent": False}
    item = {
        "analysis_type": "date_inconsistency",
        "severity": "high",
        "confidence": 90,
        "title": "Mortgage date does not match recording date",
        "observation": "Mortgage dated 03/01/2006 but recorded 05/15/2006.",
        "why_suspicious": "Dates do not line up.",
        "quoted_evidence": "Mortgage dated 03/01/2006. Recorded May 15, 2006.",
        "page": 1,
    }
    out = _finding_from_analysis(
        item, 0, result, text=MORTGAGE, source_file=Path("Mortgage.pdf")
    )
    assert out is None
