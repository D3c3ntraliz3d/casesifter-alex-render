"""Critical forgery indicators set the risk floor. Clean checks cannot pull it down.

Case 11467-2007: Verizon (Exhibit S-41), National Grid, and Citibank are the three
known forged exhibits and must score at least 85. A clean exhibit in that case
stays under 50.
"""

from __future__ import annotations

from app.engines.statement_dates import find_statement_date_conflicts
from app.finding_policy import triage_findings
from app.models import Finding
from app.report.scoring import executive_summary, risk_banner, score_risk

# NYSCEF header sitting on the line above the bill date. The old 80-character
# stamp window treated that body date as stamp furniture and dropped the catch.
S41_PAGE = """FILED: KINGS COUNTY CLERK 09/14/2009 INDEX NO. 11467/2007 NYSCEF DOC. NO. 41 RECEIVED NYSCEF: 09/14/2009
Verizon
Account number 551209
Amount due $64.18
Past due balance $10.00
Statement date April 2007
Next due date December 2011
"""

CLEAN_BILL = """Verizon
Account number 551209
Amount due $64.18
Statement date April 16, 2007
Service period April 1, 2007 - April 30, 2007
Due date May 7, 2007
"""

REDACTION_PAGE = """Redacted per 22 NYCRR §202.5(e)
Account number 100200
Amount due $40.00
Statement date April 16, 2007
Due date May 7, 2007
"""


def _finding(
    fid: str,
    *,
    severity: str = "medium",
    title: str,
    explanation: str = "",
    why: str = "",
    confidence: float = 78,
    page: int | None = None,
    analysis_type: str | None = None,
    category: str = "contextual",
    evidence: dict | None = None,
) -> Finding:
    ev = dict(evidence or {})
    if analysis_type:
        ev["analysis_type"] = analysis_type
    return Finding(
        id=fid,
        category=category,
        severity=severity,  # type: ignore[arg-type]
        title=title,
        explanation=explanation or title,
        why_suspicious=why or title,
        confidence=confidence,
        page=page,
        evidence=ev,
    )


def _overlay(page: int = 2) -> Finding:
    return _finding(
        f"img-solid-overlay-page-{page}",
        severity="high",
        title=f"Solid white overlay covering content on page-{page}",
        explanation=(
            "A near-uniform pure-white rectangle sits over darker surrounding print. "
            "That pattern matches a whiteout / paste / redaction block on a scanned statement."
        ),
        why="A flat white slab over account lines is how an edited field is hidden before print-then-scan.",
        confidence=78,
        page=page,
        category="image",
        analysis_type="graphic_manipulation",
        evidence={"print_scan_review": True, "content_hook": True, "overlay_frac": 0.12},
    )


def _info(fid: str) -> Finding:
    return _finding(
        fid,
        severity="info",
        title="Routine scan note",
        explanation="Ordinary flatbed texture.",
        confidence=20,
        category="image",
        analysis_type="graphic_manipulation",
        evidence={"scan_heavy": True},
    )


def _score(findings: list[Finding], text: str = ""):
    triaged = triage_findings(findings, "forensic", full_text=text)
    level, score = score_risk(triaged)
    summary = executive_summary(level, "exhibit.pdf", triaged)
    return triaged, level, score, summary


def test_s41_due_date_survives_nyscef_header_and_past_due_language():
    hits = find_statement_date_conflicts(S41_PAGE, scan_profile="forensic", source="Exhibit S-41")
    assert hits, "April 2007 statement with a December 2011 due date must be flagged"
    due = next(item for item in hits if "due date" in item.title.lower())
    assert due.severity == "critical"
    assert due.page == 1
    blob = f"{due.explanation} {due.evidence.get('quoted_evidence')}"
    assert "April 2007" in blob
    assert "December 2011" in blob


def test_s41_verizon_overlay_and_due_date_score_at_the_top():
    dates = find_statement_date_conflicts(S41_PAGE, scan_profile="forensic", source="Exhibit S-41")
    triaged, level, score, summary = _score([_overlay(2), *dates, _info("clean-1"), _info("clean-2")], S41_PAGE)
    assert score >= 85
    assert score >= 95
    assert level == "red"
    assert risk_banner(level, triaged) == "Critical risk — strong indicators of alteration"
    assert summary.startswith("Critical risk — strong indicators of alteration")
    assert "page 2" in summary
    assert "December 2011" in summary or "April 2007" in summary
    assert "100% forged" not in summary
    assert "proof of fraud" not in summary
    assert any(item.severity == "critical" and item.page == 2 for item in triaged)


def test_clean_checks_do_not_lower_a_critical_overlay():
    alone, _, score_alone, _ = _score([_overlay(2)])
    with_clean, _, score_clean, _ = _score([_overlay(2), *[_info(f"c{i}") for i in range(6)]])
    assert score_alone >= 85
    assert score_clean >= score_alone
    assert risk_banner("red", alone) == "High risk"
    assert "Critical risk" not in executive_summary("red", "Exhibit S-41", alone)


def test_national_grid_and_citibank_forgeries_clear_85_and_clean_exhibit_stays_under_50():
    grid = [
        _finding(
            "grid-era",
            title="National Grid did not serve Brooklyn in April 2007",
            explanation="National Grid began serving the former KeySpan Brooklyn territory in August 2007.",
            why="The named issuer did not serve that area on the statement date.",
            confidence=72,
            page=1,
            analysis_type="historical_impossibility",
            evidence={"quoted_evidence": "Statement Date: April 12, 2007. Service Address: Brooklyn, NY", "ai_generated": True},
        ),
        _finding(
            "grid-math",
            title="National Grid arithmetic does not reconcile",
            explanation="Prior balance 120.00 plus charges 40.00 does not equal amount due 184.22.",
            why="The printed total is not the sum of the lines on the statement.",
            confidence=70,
            page=2,
            analysis_type="math_error",
            evidence={"quoted_evidence": "120.00 + 40.00 = 184.22", "ai_generated": True},
        ),
    ]
    citi = [
        _finding(
            "citi-math",
            title="Citibank statement arithmetic does not reconcile",
            explanation="Opening balance 1,200.00 plus deposits 300.00 does not equal the ending balance 4,812.55.",
            why="A bank statement whose running total cannot be rebuilt is internally false.",
            confidence=74,
            page=3,
            analysis_type="math_error",
            evidence={"quoted_evidence": "1,200.00 + 300.00 = 4,812.55", "ai_generated": True},
        ),
        _finding(
            "citi-font",
            title="Citibank amount set in a different typeface",
            explanation="The amount $4,812.55 is set in a different typeface than the printed row.",
            why="The text layer in the amount field does not match the form around it.",
            confidence=68,
            page=3,
            analysis_type="font_mismatch",
            evidence={"quoted_evidence": "amount $4,812.55", "ai_generated": True},
        ),
    ]
    clean_dates = find_statement_date_conflicts(CLEAN_BILL, scan_profile="forensic", source="Exhibit S-12")
    clean = [
        *clean_dates,
        _finding(
            "img-ela-page-1",
            severity="info",
            title="Uneven recompression on scanned page",
            confidence=32,
            page=1,
            category="image",
            evidence={"scan_heavy": True, "ela_score": 20},
        ),
        _finding(
            "md-year-gap",
            severity="low",
            title="File year differs from body year",
            confidence=40,
            category="metadata",
        ),
    ]
    _, _, grid_score, grid_summary = _score(grid)
    _, _, citi_score, citi_summary = _score(citi)
    _, _, clean_score, clean_summary = _score(clean, CLEAN_BILL)
    assert grid_score >= 85
    assert citi_score >= 85
    assert clean_score < 50
    assert "100% forged" not in grid_summary
    assert "proof of fraud" not in grid_summary
    assert "100% forged" not in citi_summary
    assert "proof of fraud" not in citi_summary
    assert "Critical risk" not in clean_summary


def test_court_redaction_legend_is_not_concealment():
    overlay = _overlay(1)
    triaged, _, score, summary = _score([overlay], REDACTION_PAGE)
    assert all(item.severity != "critical" for item in triaged)
    assert score < 50
    assert "Critical risk" not in summary
    # The legend on page 1 does not excuse an overlay on a different page.
    other, _, other_score, _ = _score([_overlay(2)], REDACTION_PAGE + "\f" + CLEAN_BILL)
    assert other_score >= 85
    assert any(item.severity == "critical" and item.page == 2 for item in other)
