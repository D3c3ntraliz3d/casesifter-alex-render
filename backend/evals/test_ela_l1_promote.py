"""ELA promote requires template leftovers OR same-page L1 — never ELA alone."""

from __future__ import annotations

from app.engines.template_fabrication import review_print_scan_ela
from app.finding_policy import is_material_finding
from app.models import Finding


def _ela(*, page: int, score: float = 70.0, blob: float = 3000.0) -> Finding:
    return Finding(
        id=f"img-ela-page-{page}",
        category="image",
        severity="info",
        title=f"Uneven recompression on scanned page page-{page}",
        explanation="Localized ELA patches.",
        why_suspicious="JPEG history differs.",
        confidence=32,
        page=page,
        evidence={
            "ela_score": score,
            "largest_blob_px": blob,
            "scan_heavy": True,
            "analysis_type": "graphic_manipulation",
        },
    )


def _l1_math(page: int) -> Finding:
    return Finding(
        id="txt-math-test",
        category="contextual",
        severity="critical",
        title="Statement ending balance does not reconcile",
        explanation="Beginning 100.00 minus 10.00 plus 5.00 is 95.00, but ending is 200.00.",
        why_suspicious="Printed totals do not reconcile.",
        confidence=90,
        page=page,
        evidence={
            "analysis_type": "math_error",
            "quoted_evidence": "Beginning Balance: $100.00; Ending Balance: $200.00",
            "deterministic": True,
        },
    )


CLEAN = (
    "Con Edison statement. Account number 44122918. Amount due $140.00. "
    "Previous balance $100.00. Payments $40.00. New charges $80.00. "
    "Statement date April 16, 2007."
)

TEMPLATE = (
    "Con Edison statement. Account number [Account Number]. Amount due $140.00. "
    "Customer: Your Company Name. Previous balance $100.00. www.example.com"
)


def test_strong_ela_alone_on_clean_statement_must_suppress():
    upgraded = review_print_scan_ela([_ela(page=1)], CLEAN, scan_profile="forensic")
    assert len(upgraded) == 1
    assert upgraded[0].evidence.get("scan_heavy") is True
    assert not upgraded[0].evidence.get("print_scan_review")
    assert is_material_finding(upgraded[0]) is False


def test_strong_ela_with_template_leftovers_promotes():
    upgraded = review_print_scan_ela([_ela(page=1)], TEMPLATE, scan_profile="forensic")
    assert upgraded[0].evidence.get("print_scan_review") is True
    assert upgraded[0].evidence.get("ela_l1_gate") == "template_leftovers"
    assert is_material_finding(upgraded[0])


def test_strong_ela_with_same_page_l1_promotes():
    upgraded = review_print_scan_ela(
        [_ela(page=2), _l1_math(page=2)], CLEAN, scan_profile="forensic"
    )
    ela = next(item for item in upgraded if str(item.id).startswith("img-ela-"))
    assert ela.evidence.get("print_scan_review") is True
    assert ela.evidence.get("ela_l1_gate") == "same_page_l1"
    assert is_material_finding(ela)


def test_strong_ela_with_l1_on_other_page_must_suppress():
    upgraded = review_print_scan_ela(
        [_ela(page=1), _l1_math(page=2)], CLEAN, scan_profile="forensic"
    )
    ela = next(item for item in upgraded if str(item.id).startswith("img-ela-"))
    assert not ela.evidence.get("print_scan_review")
    assert ela.evidence.get("scan_heavy") is True
    assert is_material_finding(ela) is False


def test_soft_ela_with_same_page_l1_stays_quiet():
    soft = _ela(page=1, score=40.0, blob=1200.0)
    upgraded = review_print_scan_ela([soft, _l1_math(page=1)], CLEAN, scan_profile="forensic")
    ela = next(item for item in upgraded if str(item.id).startswith("img-ela-"))
    assert not ela.evidence.get("print_scan_review")
    assert is_material_finding(ela) is False
