"""Issuer checks and AI fact extract skip form filings; statements still fire."""

from __future__ import annotations

from app.ai_fact_extract import extract_ai_facts
from app.engines.contextual_logic import (
    _finding_from_verdict,
    _verification_record,
    build_claims,
    extract_entities,
)
from app.engines.statement_dates import find_statement_date_conflicts
from app.scan_profile import (
    PROFILE_COURT_FILING,
    PROFILE_FORENSIC,
    boilerplate_skip_reason,
    finalize_scan_profile,
    is_issuer_check_document,
)

# Synthetic NYSCEF e-filing consent (Doc 13 pattern) — no client text.
EFILING_CONSENT = """
FILED: BRONX COUNTY CLERK 01/15/2024
NYSCEF DOC. NO. 13
SUPREME COURT OF THE STATE OF NEW YORK
COUNTY OF BRONX
U.S. Bank Trust, Plaintiff,
- against -
Blue Life Dereimer LLC, Defendant.
Index No. PLACEHOLDER/2023E
NOTICE OF ELECTRONIC FILING
CONSENT TO ELECTRONIC FILING
Please take notice that this matter is subject to electronic filing.
Fay Servicing LLC appears as counsel for Plaintiff.
"""

VERIZON_STATEMENT = """
VERIZON
Account Statement
Account number 000111222
Statement Date: April 15, 2007
Service Period: 03/01/2007 - 03/31/2007
Payment Due Date: December 15, 2011
Previous balance $40.00
Payments $40.00
New charges $92.45
Amount Due: $92.45
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
"""

NATIONAL_GRID_STATEMENT = """
NATIONAL GRID
Account Statement
Statement Date: April 12, 2007
Service Period: 03/01/2007 - 03/31/2007
Due Date: April 30, 2007
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
Previous balance $100.00
Payments $100.00
New charges $184.22
Amount Due: $184.22
"""

CITIBANK_STATEMENT = """
CITIBANK
Account Statement
Account number 99887766
Statement Date: April 10, 2007
Service Period: 03/01/2007 - 03/31/2007
Due Date: April 28, 2007
Previous balance $210.00
Payments $50.00
New charges $75.00
Amount Due: $999.99
Customer: PARTY_A
"""


def test_efiling_consent_is_boilerplate():
    reason = boilerplate_skip_reason(EFILING_CONSENT, display_name="13 - Consent.pdf")
    assert reason
    assert "boilerplate" in reason


def test_efiling_consent_skips_issuer_check():
    assert not is_issuer_check_document(
        EFILING_CONSENT,
        scan_profile=PROFILE_FORENSIC,
        display_name="NYSCEF Doc 13",
    )
    entities = extract_entities(EFILING_CONSENT)
    claims = build_claims(entities, text=EFILING_CONSENT)
    assert claims == []


def test_native_boilerplate_promotes_to_court_filing():
    profile = finalize_scan_profile(
        PROFILE_FORENSIC,
        doc_kind="",
        peek_text=EFILING_CONSENT,
        suffix=".pdf",
        origin={"guess": "native-text", "scan_heavy": False},
        display_name="Consent to e-filing",
    )
    assert profile == PROFILE_COURT_FILING


def test_caption_alone_still_forensic_when_not_boilerplate():
    caption = (
        "SUPREME COURT OF THE STATE OF NEW YORK COUNTY OF KINGS\n"
        "Specialized Loan Servicing LLC, Plaintiff,\n"
        "Plaintiffs - against -\n"
        "WHEREFORE plaintiff demands judgment against the defendants."
    )
    assert boilerplate_skip_reason(caption) is None
    profile = finalize_scan_profile(
        PROFILE_FORENSIC,
        doc_kind="",
        peek_text=caption,
        suffix=".pdf",
        origin={"guess": "native-text", "scan_heavy": False},
    )
    assert profile == PROFILE_FORENSIC


def test_ai_fact_extract_skips_boilerplate_without_gemini(monkeypatch):
    monkeypatch.setenv("GEMINI_API_KEY", "test-key-not-used")
    from app import config

    monkeypatch.setattr(config.settings, "gemini_api_key", "test-key-not-used")
    facts, usage = extract_ai_facts(
        EFILING_CONSENT,
        document_label="Doc13 Consent",
        doc_kind="notice",
        scan_profile=PROFILE_FORENSIC,
        chunk_id="full",
    )
    assert facts == []
    assert str(usage.get("skipped") or "").startswith("boilerplate")
    assert usage.get("calls") == 0
    assert usage.get("chunk_id") == "full"


def test_unverifiable_is_informational_not_a_finding():
    from app.engines.contextual_logic import Claim

    claim = Claim(
        organization="Blue Life Placeholder LLC",
        location=None,
        date_iso="2023-01-01",
        date_text="January 1, 2023",
        question='Was the organization "Blue Life Placeholder LLC" in operation on 2023-01-01?',
    )
    verdict = {
        "verdict": "unverifiable",
        "model_confidence": 95.0,
        "reasoning": "Small LLC with no public footprint.",
        "basis": "general knowledge",
        "model": "gemini-test",
    }
    assert _finding_from_verdict(claim, verdict) is None
    record = _verification_record(claim, verdict)
    assert record["verdict"] == "unverifiable"
    assert record.get("informational") is True
    assert "model_confidence" not in record
    assert "95" not in str(record.get("model_confidence"))


def test_utility_statements_still_get_issuer_claims():
    assert is_issuer_check_document(VERIZON_STATEMENT, scan_profile=PROFILE_FORENSIC)
    entities = extract_entities(VERIZON_STATEMENT)
    claims = build_claims(entities, text=VERIZON_STATEMENT)
    assert any("Verizon" in c.organization for c in claims)
    assert not any("Dereimer" in c.organization for c in claims)


def test_11467_forgeries_still_caught_offline():
    """Deterministic detectors for the three forged utilities must still fire."""
    import re
    from datetime import datetime

    verizon_hits = find_statement_date_conflicts(
        VERIZON_STATEMENT, scan_profile="forensic", source="Exhibit U1"
    )
    assert verizon_hits, "Verizon due-date impossibility must fire"

    # National Grid Brooklyn before Aug 2007 market entry (same rule as the curated table).
    assert "national grid" in NATIONAL_GRID_STATEMENT.lower()
    assert "brooklyn" in NATIONAL_GRID_STATEMENT.lower()
    date_m = re.search(
        r"(?i)statement\s+date\s*:\s*([A-Za-z]+\s+\d{1,2},\s+\d{4})",
        NATIONAL_GRID_STATEMENT,
    )
    assert date_m
    stated = datetime.strptime(date_m.group(1), "%B %d, %Y").date().isoformat()
    assert stated[:7] < "2007-08", "National Grid Brooklyn Apr 2007 anachronism must fire"

    # Citibank line items do not reconcile to amount due.
    prev = float(re.search(r"Previous balance \$([\d.]+)", CITIBANK_STATEMENT).group(1))
    pays = float(re.search(r"Payments \$([\d.]+)", CITIBANK_STATEMENT).group(1))
    newc = float(re.search(r"New charges \$([\d.]+)", CITIBANK_STATEMENT).group(1))
    due = float(re.search(r"Amount Due: \$([\d.]+)", CITIBANK_STATEMENT).group(1))
    assert abs((prev - pays + newc) - due) > 1.0, "Citibank amount mismatch must fire"

    # Issuer checks still target letterhead issuers on these statements.
    for text, brand in (
        (VERIZON_STATEMENT, "Verizon"),
        (NATIONAL_GRID_STATEMENT, "National Grid"),
        (CITIBANK_STATEMENT, "Citibank"),
    ):
        claims = build_claims(extract_entities(text), text=text)
        assert any(brand.lower() in c.organization.lower() for c in claims), brand
