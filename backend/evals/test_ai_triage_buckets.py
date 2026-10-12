"""Content-based AI triage: full / light / none (never filename)."""

from __future__ import annotations

from app.ai_triage import (
    BUCKET_FULL,
    BUCKET_LIGHT,
    BUCKET_NONE,
    triage_document_ai,
)
from app.scan_profile import PROFILE_FORENSIC, is_issuer_check_document

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
"""

COURT_ORDER = """
SUPREME COURT OF THE STATE OF NEW YORK
COUNTY OF KINGS
Index No. 12345/2020
ORDERED that the motion is granted; and it is further
ORDERED, ADJUDGED and DECREED that judgment enter in favor of plaintiff
in the amount of $150,000.00.
SO ORDERED.
"""

SWORN_AFFIDAVIT = """
STATE OF NEW YORK
COUNTY OF KINGS
John Smith, being duly sworn, deposes and says:
1. I am the defendant in this action.
2. I reside at 100 Main Street, Brooklyn, NY.
3. The allegations in the complaint are false.
Sworn to before me this 12th day of March, 2024
Notary Public
"""

AOS_SWORN = """
AFFIDAVIT OF SERVICE
I, Process Server, being duly sworn, depose and say that on January 5, 2024
I served the summons and complaint on the defendant personally at 100 Main St.
Sworn to before me this 6th day of January, 2024
Notary Public
"""

UTILITY = """
VERIZON Account Statement
Account number 000111222
Statement Date: April 15, 2007
Previous balance $40.00
Amount Due: $92.45
Service Address: 100 Placeholder Street, Brooklyn, NY 11219
"""

COMPLAINT = """
SUPREME COURT OF THE STATE OF NEW YORK COUNTY OF KINGS
Plaintiffs - against -
WHEREFORE plaintiff demands judgment against the defendants
for the principal balance of $400,000 plus interest.
"""


def test_efiling_consent_is_bucket_none():
    t = triage_document_ai(EFILING_CONSENT, document_label="Doc13.pdf")
    assert t.bucket == BUCKET_NONE
    assert t.confidence == "high"


def test_filename_never_forces_none():
    t = triage_document_ai(
        SWORN_AFFIDAVIT, document_label="13 - Notice of Appearance.pdf"
    )
    assert t.bucket == BUCKET_FULL
    assert t.confidence == "high"


def test_order_is_bucket_light():
    t = triage_document_ai(COURT_ORDER)
    assert t.bucket == BUCKET_LIGHT


def test_sworn_and_utility_and_complaint_are_full():
    assert triage_document_ai(SWORN_AFFIDAVIT).bucket == BUCKET_FULL
    assert triage_document_ai(AOS_SWORN).bucket == BUCKET_FULL
    assert triage_document_ai(UTILITY).bucket == BUCKET_FULL
    assert triage_document_ai(COMPLAINT).bucket == BUCKET_FULL


def test_low_confidence_defaults_to_full():
    t = triage_document_ai("Random scanned page with no court language at all.")
    assert t.bucket == BUCKET_FULL
    assert t.confidence == "low"


def test_issuer_still_runs_on_utility_not_on_order():
    assert is_issuer_check_document(UTILITY, scan_profile=PROFILE_FORENSIC)
    assert not is_issuer_check_document(COURT_ORDER, scan_profile=PROFILE_FORENSIC)
    assert not is_issuer_check_document(EFILING_CONSENT, scan_profile=PROFILE_FORENSIC)


def test_rji_and_notice_of_appearance_are_none():
    rji = """
    REQUEST FOR JUDICIAL INTERVENTION
    Check if answer has been filed. Index No. 123/2024
    Nature of judicial intervention: Preliminary conference.
    """
    assert triage_document_ai(rji).bucket == BUCKET_NONE
    noa = """
    NOTICE OF APPEARANCE
    Please take notice that the undersigned appears as attorney for defendant
    and demands a copy of the complaint.
    """
    assert triage_document_ai(noa).bucket == BUCKET_NONE


def test_rji_with_perjury_language_is_full():
    rji = """
    REQUEST FOR JUDICIAL INTERVENTION
    I affirm under penalty of perjury that the foregoing is true.
    """
    assert triage_document_ai(rji).bucket == BUCKET_FULL
