"""Deterministic OCR date harvest must flag CIGNA / National Grid-class conflicts.

These do not depend on Gemini recalling the dates. Labeled OCR lines + tracking
chrome are enough. Filing-stamp years stay out of the comparison.
"""

from __future__ import annotations

from app.engines.statement_dates import (
    find_statement_date_conflicts,
    harvest_labeled_dates,
)
from app.finding_policy import is_material_finding

CIGNA_TRACKING_VS_PERIOD = """
CIGNA Retirement Statement
Statement Period: January 1, 2009 - February 28, 2009
Account Number: 003739620
24091148-10295-2004-07-30-N790-003739620
Beginning Balance: $12,400.00
"""

CIGNA_LABELED_PRODUCTION = """
CIGNA Retirement Statement
Statement period Jan. 1 - Feb. 28, 2009
Production tracking 05/12/2004
Account Number: 12345
Amount due $0.00
"""

NATIONAL_GRID_NEXT_READ_BEFORE_BILL = """
National Grid
Account Statement
Bill Date Jan 07 '09
Next Meter Reading Dec 06 '08
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
Amount Due: $40.00
FILED: KINGS COUNTY CLERK 08/18/2024
"""

NATIONAL_GRID_NEXT_READ_OK = """
National Grid
Account Statement
Bill Date Jan 07 '09
Next Meter Reading Feb 06 '09
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
Amount Due: $40.00
"""

ORDINARY_PERIOD_PRINT = """
Citibank checking account statement
Statement Period Mar. 16 - Apr. 16, 2007
Statement Date: Apr 18, 2007
Beginning Balance: $1,319.48
Ending Balance: $2,075.38
"""


def _titles(findings) -> list[str]:
    return [item.title for item in findings]


def test_cigna_tracking_iso_vs_2009_period_flags_production_conflict():
    hits = find_statement_date_conflicts(
        CIGNA_TRACKING_VS_PERIOD, scan_profile="forensic", source="Exhibit"
    )
    prod = [item for item in hits if "production date" in item.title.lower()]
    assert prod, _titles(hits)
    assert is_material_finding(prod[0])
    assert prod[0].evidence.get("analysis_type") == "date_inconsistency"
    assert prod[0].evidence.get("deterministic") is True
    quote = prod[0].evidence.get("quoted_evidence") or ""
    assert "2004-07-30" in quote
    assert "2009" in quote
    assert prod[0].evidence.get("left_date") == "2004-07-30"
    roles = {row["role"] for row in harvest_labeled_dates(CIGNA_TRACKING_VS_PERIOD)}
    assert "production_tracking" in roles
    assert "period_start" in roles


def test_cigna_labeled_production_vs_open_start_period():
    hits = find_statement_date_conflicts(
        CIGNA_LABELED_PRODUCTION, scan_profile="forensic", source="Exhibit"
    )
    prod = [item for item in hits if "production date" in item.title.lower()]
    assert prod, _titles(hits)
    assert "2004" in (prod[0].evidence.get("left_raw") or "")
    assert "2009" in (prod[0].evidence.get("right_raw") or "")


def test_national_grid_next_meter_before_bill_date():
    hits = find_statement_date_conflicts(
        NATIONAL_GRID_NEXT_READ_BEFORE_BILL, scan_profile="forensic", source="Exhibit"
    )
    meter = [item for item in hits if "meter reading" in item.title.lower()]
    assert meter, _titles(hits)
    assert is_material_finding(meter[0])
    assert meter[0].evidence.get("analysis_type") == "date_inconsistency"
    quote = meter[0].evidence.get("quoted_evidence") or ""
    assert "Dec 06" in quote or "06" in quote
    assert "Jan 07" in quote or "07" in quote
    # Filing stamp year must not become a production/period conflict.
    assert not any("2024" in (item.title or "") for item in hits)
    assert not any(
        "2024" in str((item.evidence or {}).get("left_date") or "")
        or "2024" in str((item.evidence or {}).get("right_date") or "")
        for item in hits
    )


def test_national_grid_next_meter_after_bill_stays_quiet():
    hits = find_statement_date_conflicts(
        NATIONAL_GRID_NEXT_READ_OK, scan_profile="forensic", source="Exhibit"
    )
    assert not [item for item in hits if "meter reading" in item.title.lower()]


def test_ordinary_statement_print_after_period_is_not_production_conflict():
    hits = find_statement_date_conflicts(
        ORDINARY_PERIOD_PRINT, scan_profile="forensic", source="Exhibit"
    )
    assert not [item for item in hits if "production date" in item.title.lower()]


def test_court_filing_profile_skips_harvest_conflicts():
    assert not find_statement_date_conflicts(
        NATIONAL_GRID_NEXT_READ_BEFORE_BILL,
        scan_profile="court_filing",
        source="Exhibit",
    )
    assert not find_statement_date_conflicts(
        CIGNA_TRACKING_VS_PERIOD,
        scan_profile="court_filing",
        source="Exhibit",
    )
