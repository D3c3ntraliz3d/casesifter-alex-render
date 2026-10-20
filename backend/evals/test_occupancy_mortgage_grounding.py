"""Occupancy-vs-mortgage gates must not fire on billing/filing/jurat mislabels.

Live 11467 packet painted many affidavits red because:
- billing periods / AOS timestamps were stored as occupancy_start
- filed/recorded/commencement stamps were stored as mortgage_origination
"""

from __future__ import annotations

from app.fact_graph import (
    entity_compatible,
    fact_conflict_eligible,
    same_nameable_fact,
)


def _occ(quote: str, value: str = "2009-06-02") -> dict:
    return {
        "key": "occupancy_start",
        "kind": "date",
        "value": value,
        "display": value,
        "quote": quote,
        "documentary": True,
        "graph": True,
    }


def _mort(
    quote: str,
    value: str = "2024-08-18",
    *,
    date_role: str | None = None,
    instrument_event: str | None = None,
) -> dict:
    rec = {
        "key": "mortgage_origination",
        "kind": "date",
        "value": value,
        "display": value,
        "quote": quote,
        "documentary": True,
        "graph": True,
    }
    if date_role:
        rec["date_role"] = date_role
    if instrument_event:
        rec["instrument_event"] = instrument_event
    return rec


def test_billing_period_not_occupancy_eligible():
    fact = _occ(
        "Billing Period: 06/02/09 to 07/02/09 7972 FISHER ISLAND DR UNIT 7972 MIAMI BEACH, FL"
    )
    assert not fact_conflict_eligible(fact)


def test_aos_timestamp_not_occupancy_eligible():
    fact = _occ(
        "That on 04/10/2007 at 5:02 AM at 346 ASHFORD ST, BROOKLYN, NY 11207 "
        "deponent served the within Summons and Verified Complaint"
    )
    assert not fact_conflict_eligible(fact)


def test_real_occupancy_still_eligible():
    fact = _occ(
        "I have occupied the Subject Property continuously as my primary residence since April 3, 2019."
    )
    assert fact_conflict_eligible(fact)


def test_filed_stamp_not_paired_with_occupancy():
    occ = _occ(
        "I have occupied the Subject Property continuously as my primary residence since April 3, 2019.",
        value="2019-04-03",
    )
    mort = _mort("Filed 07-07-2022", value="2022-07-07", date_role="filed")
    assert not entity_compatible(occ, mort)
    assert not same_nameable_fact(occ, mort)


def test_jurat_dated_line_not_paired_with_billing_occupancy():
    occ = _occ(
        "Billing Period: 06/02/09 to 07/02/09 7972 FISHER ISLAND DR UNIT 7972 MIAMI BEACH, FL"
    )
    mort = _mort(
        "notes, and the signature box. Dated: Glendale, New York August 18, 2024"
    )
    assert not entity_compatible(occ, mort)
    assert not same_nameable_fact(occ, mort)


def test_true_note_date_still_pairs_with_occupancy():
    occ = _occ(
        "I have occupied the Subject Property continuously as my primary residence since April 3, 2019.",
        value="2019-04-03",
    )
    mort = _mort(
        "Note signed by Borrower and dated August 01, 2006",
        value="2006-08-01",
        instrument_event="note",
        date_role="origination",
    )
    assert entity_compatible(occ, mort)
    assert same_nameable_fact(occ, mort)
