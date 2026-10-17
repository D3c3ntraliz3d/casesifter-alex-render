"""Signals that must fire on garbled exhibit text, not only on clean fixtures.

The curated Verizon / National Grid statements are readable. A scanned NYSCEF
packet often keeps the brand, a bill date, a (718) number, and both spellings
on one activity line, while dropping the word Brooklyn and gluing tokens.
"""

from __future__ import annotations

import re

import pytest

from app.engines.statement_body import (
    find_issuer_market_anachronisms,
    find_line_self_contradictions,
    find_statement_math_errors,
)
from app.engines.statement_dates import find_statement_date_conflicts
from app.finding_policy import is_critical_math_error, is_header_or_arrears_noise, is_material_finding

NATIONAL_GRID_BROOKLYN = """
NATIONAL GRID
Account Statement
Statement Date: April 12, 2007
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
Amount Due: $184.22
"""

NATIONAL_GRID_718 = """
nationalgrid
Bill Daie
May 06 '07 Apr 07 '07
Res. Heating Please call (718) 643-4050
Amount due $34.07
FILED: KINGS COUNTY CLERK 08/18/2024
"""

NATIONAL_GRID_UPSTATE = """
National Grid
Statement Date: April 12, 2007
Service Address: 10 Main Street, Syracuse, NY 13202
Please call (315) 460-1234
Amount due $40.00
"""

NATIONAL_GRID_AFTER_CLOSE = """
National Grid
Statement Date: September 2, 2007
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
Please call (718) 643-4050
Amount due $40.00
"""

CITI_LINE = """
Citibank checking account statement
Statement Period Mar. 16 - Apr. 16, 2007
Beginning Balance: $1,319.48
Total Subtracted/Added 5,168.38 5,924.28
Ending Balance: $2,075.38
4/03 Deposit 1,575.00
275 AVENTURA BL, AVNTURA, FL
4/11 Authorized Transfer 1,130.66
"""

CITI_ONE_SPELLING = """
Citibank checking account statement
Amount due $2,075.38
275 AVENTURA BL, AVENTURA, FL
Beginning Balance: $1,319.48
Total Subtracted 5,168.38
Total Added 5,924.28
Ending Balance: $2,075.38
"""

CITI_BROKEN_MATH = """
Citibank checking account statement
Statement Period Mar. 16 - Apr. 16, 2007
Beginning Balance: $1,319.48
Total Subtracted 5,168.38
Total Added 100.00
Ending Balance: $2,075.38
"""

VERIZON_BROKEN_FORWARD = """
Verizon
Statement Date: 04/04/07
Previous Balance $344.22
Payment Received Nov 16 -$290.00
Balance Forward $144.22
Amount Due $362.42
"""

VERIZON_PAYMENT = """
Verizon
Statement Date: 04/04/07
Previous Balance $344.22
Payment Received Nov 16 -$290.00
Balance Forward $144.22
Total New Charges due by December 29, 2011
New Charges Due: APR. 29, 2007
Amount Due $362.42
"""

VERIZON_PRIOR_YEAR = """
Verizon
Statement Date: 04/04/07
Payment Received Nov 16, 2006 -$290.00
Amount Due $100.00
"""

# Garbled NYSCEF-style OCR: glued tokens, mistyped labels, missing city word.
NATIONAL_GRID_GLUED_PHONE = """
NATIONALGRID AccountStatement
BillDaie Apr07'07
Pleas ecall(718)643-4050
Amountdue$34.07
FILED:KINGS COUNTY CLERK 08/18/2024
"""

NATIONAL_GRID_212_GLUE = """
national grid
Statment Date: May 06 '07
Res.Heating Please call (212)555-0199
AmountDue $40.00
"""

CITI_GLUED_MATH = """
Citibank checking account statement
StatementPeriod Mar.16-Apr.16,2007
BeginningBalance:$1,319.48
TotalSubtracted 5,168.38
TotalAdded 100.00
EndingBalance:$2,075.38
"""

VERIZON_GLUED_FORWARD = """
Verizon
StatementDate:04/04/07
PreviousBalance$344.22
PaymentReceived Nov 16 -$290.00
BalanceForward$144.22
AmountDue$362.42
"""

VERIZON_AMOUNT_DUE_IDENTITY = """
Verizon
Statement Date: 04/04/07
Previous Balance $54.22
Payment Received Apr 01 -$0.00
Balance Forward $54.22
New Charges $100.00
Amount Due $200.00
"""

VERIZON_DISAGREEING_AMOUNT_DUE = """
Verizon
Statement Date: 04/04/07
Balance Forward $54.22
New Charges $100.00
Amount Due $154.22
Amount Due $200.00
"""

VERIZON_RECONCILED_IDENTITY = """
Verizon
Statement Date: 04/04/07
Previous Balance $54.22
Payment Received Apr 01 -$0.00
Balance Forward $54.22
New Charges $100.00
Amount Due $154.22
"""

PSEG_LI_TOO_EARLY = """
PSEG Long Island
Statement Date: June 12, 2012
Service Address: 10 Main Street, Hempstead, NY 11550
Please call (516) 222-3333
Amount due $88.00
"""

PSEG_LI_AFTER_START = """
PSEG Long Island
Statement Date: March 2, 2015
Service Address: 10 Main Street, Hempstead, NY 11550
Please call (516) 222-3333
Amount due $88.00
"""

PSEG_LI_GLUED = """
PSEGLONGISLAND
BillDaie Jan 15 '13
Please call(631)555-1212
Amountdue$55.00
"""


def _types(findings) -> set[str]:
    return {str((item.evidence or {}).get("analysis_type") or "") for item in findings}


def test_national_grid_brooklyn_april_2007_flags():
    hits = find_issuer_market_anachronisms(
        NATIONAL_GRID_BROOKLYN, scan_profile="forensic", source="statement"
    )
    assert hits
    blob = hits[0].evidence.get("quoted_evidence") or ""
    assert "National Grid" in blob or "NATIONAL GRID" in blob
    assert "Brooklyn" in blob
    assert "2007" in blob
    assert hits[0].evidence.get("analysis_type") == "historical_impossibility"
    assert is_material_finding(hits[0])
    assert "math_error" not in _types(hits)


def test_national_grid_718_without_brooklyn_quotes_only_printed_tokens():
    hits = find_issuer_market_anachronisms(
        NATIONAL_GRID_718, scan_profile="forensic", source="statement"
    )
    assert hits, "brand + (718) + Apr 07 '07 must flag"
    quote = hits[0].evidence.get("quoted_evidence") or ""
    assert "nationalgrid" in quote.lower()
    assert "718" in quote
    assert "Apr 07" in quote
    # The city word is not in this text layer. Do not invent it as a quote.
    assert "brooklyn" not in quote.lower()
    assert is_header_or_arrears_noise(hits[0]) is False


@pytest.mark.parametrize(
    "fixture",
    [
        NATIONAL_GRID_718,
        NATIONAL_GRID_GLUED_PHONE,
        NATIONAL_GRID_212_GLUE,
    ],
    ids=["718_readable", "718_glued", "212_glue"],
)
def test_garbled_national_grid_ocr_shapes_still_fire(fixture):
    hits = find_issuer_market_anachronisms(fixture, scan_profile="forensic", source="statement")
    assert hits, "garbled brand + NYC number + pre-close bill date must flag"
    quote = (hits[0].evidence.get("quoted_evidence") or "").lower()
    assert "national" in quote or "nationalgrid" in quote.replace(" ", "")
    assert re.search(r"718|212", quote)
    assert is_material_finding(hits[0])


def test_national_grid_upstate_and_after_closing_stay_quiet():
    assert not find_issuer_market_anachronisms(
        NATIONAL_GRID_UPSTATE, scan_profile="forensic", source="statement"
    )
    assert not find_issuer_market_anachronisms(
        NATIONAL_GRID_AFTER_CLOSE, scan_profile="forensic", source="statement"
    )


def test_pseg_long_island_before_operator_start_flags_and_after_stays_quiet():
    early = find_issuer_market_anachronisms(
        PSEG_LI_TOO_EARLY, scan_profile="forensic", source="statement"
    )
    assert early
    assert early[0].evidence.get("issuer_rule") == "pseg_long_island"
    assert is_material_finding(early[0])
    glued = find_issuer_market_anachronisms(
        PSEG_LI_GLUED, scan_profile="forensic", source="statement"
    )
    assert glued, "glued PSEG + (631) + 2013 bill date must flag"
    assert not find_issuer_market_anachronisms(
        PSEG_LI_AFTER_START, scan_profile="forensic", source="statement"
    )


def test_same_line_aventura_spellings_flag_and_reconciled_math_does_not():
    hits = find_line_self_contradictions(CITI_LINE, scan_profile="forensic", source="statement")
    assert hits
    quote = hits[0].evidence.get("quoted_evidence") or ""
    assert "AVENTURA" in quote
    assert "AVNTURA" in quote
    assert "math_error" not in _types(hits)
    dates = find_statement_date_conflicts(CITI_LINE, scan_profile="forensic", source="statement")
    assert "math_error" not in _types(dates)
    # Reconciled beginning/ending + subtracted/added must stay quiet.
    assert not find_statement_math_errors(CITI_LINE, scan_profile="forensic", source="statement")
    assert not find_statement_math_errors(
        CITI_ONE_SPELLING, scan_profile="forensic", source="statement"
    )
    assert not find_line_self_contradictions(
        CITI_ONE_SPELLING, scan_profile="forensic", source="statement"
    )


def test_citibank_broken_ending_balance_flags_math_error():
    hits = find_statement_math_errors(CITI_BROKEN_MATH, scan_profile="forensic", source="statement")
    assert hits
    assert "math_error" in _types(hits)
    assert is_material_finding(hits[0])
    assert is_critical_math_error(hits[0])
    quote = hits[0].evidence.get("quoted_evidence") or ""
    assert "Beginning Balance" in quote
    assert "Ending Balance" in quote


def test_verizon_broken_balance_forward_flags_math_error():
    hits = find_statement_math_errors(
        VERIZON_BROKEN_FORWARD, scan_profile="forensic", source="statement"
    )
    assert hits
    assert "math_error" in _types(hits)
    assert is_material_finding(hits[0])


def test_glued_citibank_and_verizon_math_still_fire():
    citi = find_statement_math_errors(CITI_GLUED_MATH, scan_profile="forensic", source="statement")
    assert citi and "math_error" in _types(citi)
    quote = citi[0].evidence.get("quoted_evidence") or ""
    assert re.search(r"\d", quote)
    verizon = find_statement_math_errors(
        VERIZON_GLUED_FORWARD, scan_profile="forensic", source="statement"
    )
    assert verizon and "math_error" in _types(verizon)


def test_amount_due_period_identity_and_disagreeing_totals():
    broken = find_statement_math_errors(
        VERIZON_AMOUNT_DUE_IDENTITY, scan_profile="forensic", source="statement"
    )
    assert broken
    assert any("amount due" in (item.title or "").lower() for item in broken)
    quote = broken[0].evidence.get("quoted_evidence") or ""
    assert "54.22" in quote and "100.00" in quote and "200.00" in quote
    assert is_critical_math_error(broken[0])

    disagree = find_statement_math_errors(
        VERIZON_DISAGREEING_AMOUNT_DUE, scan_profile="forensic", source="statement"
    )
    assert disagree
    assert any("disagreeing" in (item.title or "").lower() for item in disagree)

    assert not find_statement_math_errors(
        VERIZON_RECONCILED_IDENTITY, scan_profile="forensic", source="statement"
    )


def test_undated_payment_survives_glued_statement_date_label():
    glued = """
Verizon
StatementDate:04/04/07
PaymentReceived Nov 16 -$290.00
AmountDue $100.00
"""
    hits = find_statement_date_conflicts(glued, scan_profile="forensic", source="statement")
    pay = [item for item in hits if "payment date" in item.title.lower()]
    assert pay, [item.title for item in hits]


def test_s41_three_families_fire_on_combined_packet():
    """Verizon dates + Grid era + Citi line/math must all be present on one packet text."""
    packet = "\f".join([VERIZON_PAYMENT, NATIONAL_GRID_718, CITI_LINE, CITI_BROKEN_MATH])
    dates = find_statement_date_conflicts(packet, scan_profile="forensic", source="S-41")
    grid = find_issuer_market_anachronisms(packet, scan_profile="forensic", source="S-41")
    lines = find_line_self_contradictions(packet, scan_profile="forensic", source="S-41")
    math = find_statement_math_errors(packet, scan_profile="forensic", source="S-41")
    assert any("payment date" in (item.title or "").lower() for item in dates)
    assert grid and grid[0].evidence.get("analysis_type") == "historical_impossibility"
    assert lines and "line_self_contradiction" in _types(lines)
    assert math and "math_error" in _types(math)
    families = {
        "date_inconsistency",
        "historical_impossibility",
        "line_self_contradiction",
        "math_error",
    }
    got = _types(dates) | _types(grid) | _types(lines) | _types(math)
    assert families <= got, got


def test_undated_november_payment_conflicts_with_april_statement_date():
    hits = find_statement_date_conflicts(
        VERIZON_PAYMENT, scan_profile="forensic", source="statement"
    )
    pay = [item for item in hits if "payment date" in item.title.lower()]
    assert pay, [item.title for item in hits]
    quote = pay[0].evidence.get("quoted_evidence") or ""
    assert "Nov 16" in quote
    assert "04/04/07" in quote
    assert "2007-11-16" not in quote
    assert "November 16, 2007" not in quote
    due_blob = " ".join(
        str(item.evidence.get("quoted_evidence") or "")
        for item in hits
        if "due date" in item.title.lower()
    )
    assert "2011" in due_blob
    assert "APR. 29, 2007" in due_blob


def test_prior_year_november_payment_is_not_flagged():
    hits = find_statement_date_conflicts(
        VERIZON_PRIOR_YEAR, scan_profile="forensic", source="statement"
    )
    assert not [item for item in hits if "payment date" in item.title.lower()]


def test_visual_page_read_flags_undated_payment_and_row_color():
    from app.engines.statement_visual import findings_from_page_read
    from app.finding_policy import is_material_finding

    hits = findings_from_page_read(
        {
            "statement_date_as_printed": "04/04/07",
            "payment_received_as_printed": "Payment Received Nov 16",
            "payment_prints_year": False,
            "row_type_or_color_mismatch": False,
            "row_quote": "",
            "row_note": "",
        },
        5,
    )
    assert len(hits) == 1
    quote = hits[0].evidence.get("quoted_evidence") or ""
    assert "Nov 16" in quote
    assert "04/04/07" in quote
    assert "2007-11-16" not in quote
    assert "November 16, 2007" not in quote
    assert is_material_finding(hits[0])

    invented = findings_from_page_read(
        {
            "statement_date_as_printed": "04/04/07",
            "payment_received_as_printed": "Nov 16, 2007",
            "payment_prints_year": False,
            "row_type_or_color_mismatch": False,
            "row_quote": "",
            "row_note": "",
        },
        5,
    )
    assert invented == []

    prior = findings_from_page_read(
        {
            "statement_date_as_printed": "04/04/07",
            "payment_received_as_printed": "Nov 16, 2006",
            "payment_prints_year": True,
            "row_type_or_color_mismatch": False,
            "row_quote": "",
            "row_note": "",
        },
        5,
    )
    assert prior == []

    row = findings_from_page_read(
        {
            "statement_date_as_printed": "",
            "payment_received_as_printed": "",
            "payment_prints_year": False,
            "row_type_or_color_mismatch": True,
            "row_quote": "275 AVENTURA BL, AVNTURA, FL",
            "row_note": "The bank-detail rows use different sizes and colors.",
        },
        3,
    )
    assert len(row) == 1
    assert row[0].evidence.get("quoted_evidence") == "275 AVENTURA BL, AVNTURA, FL"
    assert is_material_finding(row[0])

    compared = findings_from_page_read(
        {
            "descriptions": [
                {
                    "date_as_printed": "4/02",
                    "description_as_printed": "Deposit",
                    "letter_size_vs_date": "same",
                    "ink_vs_date": "lighter",
                },
                {
                    "date_as_printed": "4/03",
                    "description_as_printed": "Deposit 2750 AVENTURA BL, AVNTURA, FL",
                    "letter_size_vs_date": "same",
                    "ink_vs_date": "lighter",
                },
                {
                    "date_as_printed": "4/11",
                    "description_as_printed": "Authorized Transfer Countrywide MORTGAGE",
                    "letter_size_vs_date": "same",
                    "ink_vs_date": "same",
                },
            ]
        },
        3,
    )
    assert len(compared) == 1
    assert compared[0].evidence.get("quoted_evidence") == "Deposit 2750 AVENTURA BL, AVNTURA, FL"
    assert "lighter" in (compared[0].why_suspicious or "").lower()
    assert "type size" not in (compared[0].why_suspicious or "").lower()
    assert is_material_finding(compared[0])

    every_row = findings_from_page_read(
        {
            "descriptions": [
                {
                    "date_as_printed": "4/02",
                    "description_as_printed": "First long description",
                    "letter_size_vs_date": "smaller",
                    "ink_vs_date": "lighter",
                },
                {
                    "date_as_printed": "4/03",
                    "description_as_printed": "Second long description",
                    "letter_size_vs_date": "smaller",
                    "ink_vs_date": "lighter",
                },
            ]
        },
        3,
    )
    assert every_row == []

    texture = findings_from_page_read(
        {
            "statement_date_as_printed": "",
            "payment_received_as_printed": "",
            "payment_prints_year": False,
            "row_type_or_color_mismatch": True,
            "row_quote": "ordinary flatbed noise across the page",
            "row_note": "Whole-page scan texture only.",
        },
        3,
    )
    assert texture == []


def test_localized_row_chrome_ships_and_whole_page_texture_does_not():
    from app.engines.contextual_logic import _finding_from_analysis

    kept = _finding_from_analysis(
        {
            "analysis_type": "font_mismatch",
            "title": "Mixed type in the activity rows",
            "severity": "high",
            "observation": "The bank detail rows use different sizes and colors.",
            "why_suspicious": "The change is inside the activity rows, not the scan texture.",
            "confidence": 84,
            "page": 3,
            "quoted_evidence": "275 AVENTURA BL, AVNTURA, FL",
        },
        0,
        {"model": "gemini-test"},
        text="Citibank checking",
    )
    assert kept is not None
    assert is_material_finding(kept)
    dropped = _finding_from_analysis(
        {
            "analysis_type": "font_mismatch",
            "title": "Page looks soft",
            "severity": "medium",
            "observation": "The whole page scan texture has a slightly different font feel.",
            "why_suspicious": "Entire page flatbed softness.",
            "confidence": 80,
            "page": 1,
            "quoted_evidence": "ordinary flatbed noise on the page",
        },
        1,
        {"model": "gemini-test"},
        text="Citibank checking",
    )
    assert dropped is None
