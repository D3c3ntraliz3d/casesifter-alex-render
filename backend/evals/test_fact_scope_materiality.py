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
