"""Scoping keys: CEMA principals, assignment dates, and per-affidavit venues."""

from __future__ import annotations

from app.case_consistency import (
    _internal_fact_conflicts,
    aggregate_fact_conflicts,
)
from app.fact_extract import extract_facts
from app.fact_graph import (
    cluster_key,
    entity_compatible,
    facts_compatible,
    same_nameable_fact,
)


def test_cema_component_principals_do_not_conflict():
    text = (
        "CEMA. Prior mortgage principal amount $320,000.00.\n"
        "Gap mortgage principal amount $85,000.00.\n"
        "Consolidated principal amount $405,000.00.\n"
    )
    facts = [f for f in extract_facts(text) if f.get("key") == "stated_principal"]
    roles = {str(f.get("principal_role") or "") for f in facts}
    assert "prior" in roles or "gap" in roles or "consolidated" in roles
    assert len({cluster_key(f) for f in facts}) >= 2
    for i, left in enumerate(facts):
        for right in facts[i + 1 :]:
            if str(left.get("principal_role")) != str(right.get("principal_role")):
                assert not same_nameable_fact(left, right)
                assert facts_compatible(left, right)
    assert not _internal_fact_conflicts("CEMA", extract_facts(text))


def test_same_role_principal_still_conflicts():
    left = {
        "key": "stated_principal",
        "principal_role": "note",
        "value": 500000.0,
        "display": "$500,000.00",
        "graph": True,
        "documentary": True,
        "quote": "principal amount $500,000.00",
    }
    right = {
        "key": "stated_principal",
        "principal_role": "note",
        "value": 400000.0,
        "display": "$400,000.00",
        "graph": True,
        "documentary": True,
        "quote": "principal amount $400,000.00",
    }
    assert same_nameable_fact(left, right)
    assert not facts_compatible(left, right)


def test_assignment_chain_dates_do_not_conflict_with_mortgage():
    text = (
        "Mortgage dated January 15, 2008.\n"
        "Assignment of Mortgage dated March 1, 2015 to First National Bank.\n"
        "Assignment of Mortgage dated June 12, 2019 to Servicer Holdings LLC.\n"
    )
    facts = [f for f in extract_facts(text) if f.get("key") == "mortgage_origination"]
    assert len(facts) >= 2
    events = {str(f.get("instrument_event") or "") for f in facts}
    assert "assignment" in events
    assert "mortgage" in events or "note" in events
    for i, left in enumerate(facts):
        for right in facts[i + 1 :]:
            if cluster_key(left) != cluster_key(right):
                assert not same_nameable_fact(left, right)
    assert not _internal_fact_conflicts("AOM chain", extract_facts(text))


def test_same_instrument_two_origination_dates_still_conflict():
    text = (
        "The mortgage was executed on May 27, 2020.\n"
        "Date of the Mortgage in April 6, 2022.\n"
    )
    facts = extract_facts(text)
    hits = _internal_fact_conflicts("Note binder", facts)
    assert any(
        (getattr(f, "evidence", None) or {}).get("field") == "mortgage_origination"
        or "origination" in (f.title or "").lower()
        for f in hits
    )


def test_execution_venue_cross_affidavit_quiet():
    docs = [
        {
            "id": "aos-ny",
            "name": "AOS New York",
            "text": (
                "AFFIDAVIT OF SERVICE\nI, Timothy Brailsford, being duly sworn\n"
                "State of New York, County of New York\nSworn on April 9, 2007\n"
            ),
        },
        {
            "id": "aos-bx",
            "name": "AOS Bronx",
            "text": (
                "AFFIDAVIT OF SERVICE\nI, Timothy Brailsford, being duly sworn\n"
                "State of New York, County of Bronx\nSworn on April 9, 2007\n"
            ),
        },
    ]
    snaps = [
        {
            "document_id": d["id"],
            "display_name": d["name"],
            "facts": extract_facts(d["text"]),
            "text_excerpt": d["text"],
        }
        for d in docs
    ]
    conflicts = [
        row
        for row in aggregate_fact_conflicts(snaps)
        if row.get("severity") in {"high", "medium"}
        and row.get("field") == "execution_venue"
    ]
    assert not conflicts


def test_execution_venue_multi_affidavit_package_quiet():
    """Summons & Complaint packages often bind several affidavits — venues are per jurat."""
    text = (
        "SUMMONS AND COMPLAINT\n"
        "AFFIDAVIT OF SERVICE\nI, Timothy Brailsford, being duly sworn\n"
        "State of New York, County of New York\nSworn on April 9, 2007\n"
        "\n"
        "AFFIDAVIT OF MERIT\nI, Jane Servicer, being duly sworn\n"
        "State of New York, County of Bronx\nSworn on April 9, 2007\n"
    )
    facts = extract_facts(text)
    venues = [f for f in facts if f.get("key") == "execution_venue"]
    assert len(venues) >= 2
    units = {str(v.get("affidavit_unit") or "") for v in venues}
    assert len(units) >= 2
    assert not entity_compatible(venues[0], venues[1])
    assert not _internal_fact_conflicts("Exhibit 41 Summons package", facts)


def test_execution_venue_same_affidavit_conflicts():
    text = (
        "AFFIDAVIT OF SERVICE\nI, Timothy Brailsford, being duly sworn\n"
        "State of New York, County of Suffolk\nSworn on April 9, 2007\n"
        "Also executed in Kings County on April 9, 2007\n"
    )
    facts = extract_facts(text)
    venues = [f for f in facts if f.get("key") == "execution_venue"]
    assert len(venues) >= 2
    assert entity_compatible(venues[0], venues[1])
    assert not facts_compatible(venues[0], venues[1])
    hits = _internal_fact_conflicts("AOS", facts)
    assert hits


def test_default_rate_twenty_four_percent_not_note_conflict():
    text = (
        "The Note provides for interest at the rate of 5.125% per annum.\n"
        "Upon an Event of Default, interest at the rate of TWENTY-FOUR PERCENT (24%) "
        "per annum shall accrue until the Default is cured.\n"
    )
    facts = [f for f in extract_facts(text) if f.get("key") == "stated_interest_rate"]
    purposes = {str(f.get("rate_purpose") or f.get("rate_role") or "") for f in facts}
    assert "note" in purposes
    assert "default" in purposes
    note = next(f for f in facts if f.get("rate_purpose") == "note")
    default = next(f for f in facts if f.get("rate_purpose") == "default")
    assert abs(float(note["value"]) - 5.125) < 0.01
    assert abs(float(default["value"]) - 24.0) < 0.01
    assert not same_nameable_fact(note, default)
    assert not _internal_fact_conflicts("Note", extract_facts(text))


def test_cema_components_sum_is_confirmation_not_conflict():
    text = (
        "Prior mortgage principal amount $300,000.00.\n"
        "Gap mortgage principal amount $75,000.00.\n"
        "Second mortgage principal amount $25,000.00.\n"
        "Consolidated principal amount $400,000.00.\n"
    )
    assert not _internal_fact_conflicts("CEMA", extract_facts(text))


def test_principal_cap_language_is_not_conflict():
    text = (
        "The principal amount of the Note is $400,000.00.\n"
        "The mortgage secures the principal amount of the Note or up to $500,000.00.\n"
    )
    facts = extract_facts(text)
    assert any(
        fact.get("key") == "stated_principal"
        and fact.get("principal_role") == "cap"
        for fact in facts
    )
    assert not _internal_fact_conflicts("Mortgage and Note", facts)


def test_cross_instrument_rates_are_quiet():
    facts = [
        {
            "key": "stated_interest_rate",
            "value": 4.0,
            "display": "4%",
            "quote": "first mortgage interest rate of 4%",
            "graph": True,
            "documentary": True,
            "instrument_scope": "prior",
            "rate_purpose": "note",
        },
        {
            "key": "stated_interest_rate",
            "value": 8.0,
            "display": "8%",
            "quote": "second mortgage interest rate of 8%",
            "graph": True,
            "documentary": True,
            "instrument_scope": "second",
            "rate_purpose": "note",
        },
    ]
    assert not same_nameable_fact(facts[0], facts[1])
    assert facts_compatible(facts[0], facts[1])
    assert not _internal_fact_conflicts("Two mortgages", facts)


def _timeline_snapshot(text: str) -> list[dict]:
    return [
        {
            "document_id": "timeline",
            "display_name": "Timeline",
            "facts": extract_facts(text),
            "text_excerpt": text,
        }
    ]


def test_assignment_before_commencement_is_timeline_only():
    text = (
        "Assignment of Mortgage effective as of January 1, 2020.\n"
        "Assignment of Mortgage executed on February 1, 2020.\n"
        "The foreclosure action commenced on March 1, 2020.\n"
    )
    facts = extract_facts(text)
    roles = {fact.get("date_role") for fact in facts}
    assert {"assignment_effective", "assignment_execution", "commencement"} <= roles
    assert any(fact.get("timeline_context") for fact in facts)
    assert not any(
        row.get("field") == "standing"
        for row in aggregate_fact_conflicts(_timeline_snapshot(text))
    )


def test_assignment_after_commencement_is_flagged():
    text = (
        "The foreclosure action commenced on March 1, 2020.\n"
        "Assignment of Mortgage executed on April 1, 2020.\n"
    )
    conflicts = aggregate_fact_conflicts(_timeline_snapshot(text))
    assert any(
        row.get("field") == "standing"
        and row.get("severity") == "high"
        for row in conflicts
    )
