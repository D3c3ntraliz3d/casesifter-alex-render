"""Critical forgery indicators set the risk floor. Clean checks cannot pull it down.

Case 11467-2007: Verizon (Exhibit S-41), National Grid, and Citibank are the three
known forged exhibits and must score at least 85. A clean exhibit in that case
stays under 50.
"""

from __future__ import annotations

from app.engines.statement_dates import find_statement_date_conflicts
from app.finding_policy import is_material_finding, triage_findings
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

# Genuine 2007 bill filed years later. The 2023 NYSCEF stamp must not become
# a statement date or due date and must not create a temporal impossibility.
GENUINE_BILL_WITH_2023_STAMP = """FILED: BRONX COUNTY CLERK 03/15/2023 10:22 AM
INDEX NO. 820292/2023E
NYSCEF DOC. NO. 41
RECEIVED NYSCEF: 03/15/2023
Con Edison
Account number 44122918
Amount due $92.40
Statement date April 16, 2007
Service period April 1, 2007 - April 30, 2007
Due date May 16, 2007
"""

# Ordinary arrears language with a coherent current bill cycle.
GENUINE_BILL_WITH_PAST_DUE = """Verizon
Account number 551209
Amount due $214.18
Previous balance $140.00
Past due amount $140.00
Payment received $0.00
Statement date June 5, 2023
Service period May 1, 2023 - May 31, 2023
Due date June 26, 2023
The installment payment is still due from the March 2023 billing cycle.
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


def test_s41_garbled_due_by_year_still_conflicts_with_coupon():
    """The real S-41 text layer keeps 'due by … 2011' and 'APR. 29, 2007'."""
    garbled = """
New charges due by oncemas a 2011 sas.20
Amount Due $362.42
New Charges Due: APR. 29, 2007
Amount Due $362.42
"""
    hits = find_statement_date_conflicts(garbled, scan_profile="forensic", source="Exhibit S-41")
    assert hits, "2011 due-by year and an April 2007 coupon date must be flagged"
    blob = " ".join(f"{item.explanation} {item.evidence.get('quoted_evidence')}" for item in hits)
    assert "2011" in blob
    assert "APR. 29, 2007" in blob


def test_readable_due_by_december_2011_conflicts_with_april_2007():
    text = """
Verizon
Statement date 04/04/07
Total New Charges due by December 29, 2011
Amount Due $362.42
"""
    hits = find_statement_date_conflicts(text, scan_profile="forensic", source="Exhibit S-41")
    assert hits
    blob = " ".join(f"{item.explanation} {item.evidence.get('quoted_evidence')}" for item in hits)
    assert "04/04/07" in blob
    assert "December 29, 2011" in blob


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
    assert score == 100
    assert level == "red"
    assert risk_banner(level, triaged) == "Critical risk — strong indicators of alteration"
    assert summary.startswith("Critical risk — strong indicators of alteration")
    assert "page 2" in summary
    assert "December 2011" in summary or "April 2007" in summary
    assert "100% forged" not in summary
    assert "proof of fraud" not in summary
    assert any(item.severity == "critical" and item.page == 2 for item in triaged)


def test_clean_checks_do_not_lower_a_forged_exhibit():
    alone, _, score_alone, summary_alone = _score([_overlay(2)])
    with_clean, _, score_clean, _ = _score([_overlay(2), *[_info(f"c{i}") for i in range(6)]])
    assert score_alone == 100
    assert score_clean == 100
    assert risk_banner("red", alone) == "Critical risk — strong indicators of alteration"
    assert summary_alone.startswith("Critical risk — strong indicators of alteration")


def test_s71_style_fraudulently_edited_utility_bill_scores_100():
    """Exhibit S_71 pattern: tainted forged-statement gotcha must floor at 100."""
    forged = _finding(
        "ctx-gotcha-s71",
        severity="high",
        title="Exhibit appears fraudulently edited",
        explanation=(
            "The Account Information block lists the 'Maturity date (month/year)' as "
            "'12/36' on a billing statement dated '11/18/24'."
        ),
        why="A mortgage statement with an impossible maturity date is not a genuine issuer print.",
        confidence=90,
        page=1,
        category="gotcha",
        analysis_type="date_inconsistency",
        evidence={
            "gotcha": True,
            "verified": True,
            "tainted_support": True,
            "quoted_evidence": "Maturity date (month/year) 12/36",
            "material": True,
            "evidence_tier": 1,
        },
    )
    soft = _finding(
        "ctx-ai-maturity",
        severity="medium",
        title="Date inconsistency: Wells Fargo Mortgage Statement Maturity Date Anomaly on Page 1",
        explanation="Maturity date (month/year) 12/36 on a statement dated 11/18/24.",
        why="Issuer statements do not print an impossible maturity month.",
        confidence=60,
        page=1,
        analysis_type="date_inconsistency",
        evidence={"quoted_evidence": "Maturity date (month/year) 12/36", "ai_generated": True},
    )
    triaged, level, score, summary = _score([forged, soft, _info("clean-1")])
    assert score == 100
    assert level == "red"
    assert risk_banner(level, triaged) == "Critical risk — strong indicators of alteration"
    assert any((item.evidence or {}).get("tainted_support") for item in triaged)
    assert "100% forged" not in summary
    assert "proof of fraud" not in summary


def test_s71_solo_statement_finding_scores_100_without_tainted_gotcha():
    """A cold standalone rescan still floors when Gemini flags the forged statement."""
    soft = _finding(
        "ctx-ai-maturity",
        severity="medium",
        title="Date inconsistency: Wells Fargo Mortgage Statement Maturity Date Anomaly on Page 1",
        explanation=(
            "The Account Information block lists the Maturity date (month/year) as 12/36 "
            "on a billing statement dated 11/18/24."
        ),
        why="Issuer statements do not print an impossible maturity month.",
        confidence=60,
        page=1,
        analysis_type="date_inconsistency",
        evidence={
            "quoted_evidence": "Maturity date (month/year) 12/36. Statement date 11/18/24.",
            "ai_generated": True,
        },
    )
    # Weighted medium alone used to land near Moderate (~10–58). Must be 100.
    triaged, level, score, summary = _score([soft, _info("clean-a"), _info("clean-b")])
    assert score == 100, score
    assert level == "red"
    assert risk_banner(level, triaged) == "Critical risk — strong indicators of alteration"
    assert any(item.severity == "critical" for item in triaged)
    assert "100% forged" not in summary


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
    assert grid_score == 100
    assert citi_score == 100
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
    assert other_score == 100
    assert any(item.severity == "critical" and item.page == 2 for item in other)


def test_nyscef_stamp_date_never_becomes_statement_or_due_date():
    """A genuine old bill filed with a 2023 stamp must stay quiet."""
    hits = find_statement_date_conflicts(
        GENUINE_BILL_WITH_2023_STAMP,
        scan_profile="forensic",
        source="Exhibit S-12",
    )
    assert hits == [], [item.title for item in hits]
    # Stamp-only packaging findings must not floor the score either.
    packaging = [
        _finding(
            "md-year-gap",
            severity="low",
            title="File year differs from body year",
            explanation="NYSCEF filing year 2023 vs statement date April 16, 2007.",
            why="Ordinary e-filing of an older exhibit.",
            confidence=50,
            category="metadata",
        ),
        _finding(
            "txt-date-vs-file",
            severity="medium",
            title="Body date older than file date",
            explanation="Statement date April 16, 2007; FILED stamp March 15, 2023.",
            why="Courts routinely file older bills years later.",
            confidence=55,
            category="text",
            analysis_type="date_inconsistency",
            evidence={"quoted_evidence": "FILED: 03/15/2023 vs Statement date April 16, 2007"},
        ),
    ]
    triaged, _, score, summary = _score([*hits, *packaging], GENUINE_BILL_WITH_2023_STAMP)
    assert score < 50, score
    assert all(item.severity != "critical" for item in triaged)
    assert not any(
        (item.evidence or {}).get("analysis_type") == "date_inconsistency"
        and item.severity in {"medium", "high", "critical"}
        and is_material_finding(item)
        for item in triaged
    )
    assert "Critical risk" not in summary


def test_past_due_arrears_wording_with_normal_dates_is_not_critical():
    """Previous balance / past due on a coherent current bill is not forgery."""
    hits = find_statement_date_conflicts(
        GENUINE_BILL_WITH_PAST_DUE,
        scan_profile="forensic",
        source="Exhibit S-18",
    )
    assert hits == [], [item.title for item in hits]
    soft = [
        _finding(
            "img-ela-page-1",
            severity="info",
            title="Uneven recompression on scanned page",
            confidence=32,
            page=1,
            category="image",
            evidence={"scan_heavy": True, "ela_score": 22},
        ),
        _finding(
            "ctx-arrears-note",
            severity="medium",
            title="Past-due balance cited on current statement",
            explanation="Past due amount $140.00 from the March 2023 billing cycle.",
            why="Ordinary arrears language on a utility bill.",
            confidence=60,
            analysis_type="date_inconsistency",
            evidence={
                "quoted_evidence": "Past due amount $140.00. The installment payment is still due from March 2023.",
                "ai_generated": True,
            },
        ),
    ]
    triaged, _, score, summary = _score([*hits, *soft], GENUINE_BILL_WITH_PAST_DUE)
    assert all(item.severity != "critical" for item in triaged), [
        (item.severity, item.title) for item in triaged
    ]
    assert score < 50, score
    assert "Critical risk" not in summary
