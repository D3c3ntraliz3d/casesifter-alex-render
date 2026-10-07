"""Mortgage/note conflicts must not use employment interrupt copy."""

from __future__ import annotations

from app.case_consistency import (
    _date_conflict_materiality,
    _score_pair,
    dedupe_gotchas,
    finding_stable_key,
    scrub_misattributed_employment_copy,
)


def test_mortgage_origination_materiality_has_no_employment_language():
    left = {
        "key": "mortgage_origination",
        "kind": "date",
        "label": "Mortgage origination date",
        "value": "2020-05-27",
        "display": "05/27/2020",
        "month_key": "2020-05",
        "documentary": True,
        "graph": True,
        "quote": "dated May 27, 2020",
    }
    right = {
        "key": "mortgage_origination",
        "kind": "date",
        "label": "Mortgage origination date",
        "value": "2022-04-06",
        "display": "April 6, 2022",
        "month_key": "2022-04",
        "documentary": True,
        "graph": True,
        "quote": "Note dated April 6, 2022",
    }
    row = _score_pair(left, right, "mortgage_origination")
    assert row is not None
    assert row.get("flag") == "red"
    text = f"{row.get('materiality') or ''}"
    assert "employer" not in text.lower()
    assert "employment" not in text.lower()
    assert "mortgage" in text.lower() or "note" in text.lower()


def test_date_conflict_helper_attribute_aware():
    emp = _date_conflict_materiality("employment_start", "Employment start", documentary=True)
    mort = _date_conflict_materiality("mortgage_origination", "Mortgage origination date", documentary=True)
    assert "employment" in emp.lower()
    assert "employer" not in mort.lower()
    assert "employment" not in mort.lower()


def test_scrub_strips_employment_boilerplate_from_mortgage_finding():
    item = {
        "field": "mortgage_origination",
        "explanation": (
            "This file states “05/27/2020” while “Note” states “April 6, 2022”. "
            "Discrepancy: 05/27/2020 vs April 6, 2022. Same person/employer with "
            "incompatible employment dates in documentary exhibits is a credibility interrupt."
        ),
        "evidence": {
            "field": "mortgage_origination",
            "fact_named": "Mortgage origination date",
            "analysis_type": "fact_conflict",
            "documentary": True,
        },
    }
    scrubbed = scrub_misattributed_employment_copy(item)
    blob = f"{scrubbed.get('explanation')} {scrubbed.get('attorney_why')}"
    assert "employer" not in blob.lower()
    assert "employment" not in blob.lower()


def test_finding_stable_key_dedupes_same_fact_pair():
    a = {
        "id": "1",
        "title": "RED FLAG: Mortgage origination date does not match “Note”",
        "evidence": {
            "field": "mortgage_origination",
            "analysis_type": "fact_conflict",
            "claim_quote": "05/27/2020",
            "evidence_quote": "April 6, 2022",
            "fact_named": "Mortgage origination date",
        },
    }
    b = {
        "id": "2",
        "title": "RED FLAG: Mortgage origination date does not match “Mortgage”",
        "evidence": {
            "field": "mortgage_origination",
            "analysis_type": "fact_conflict",
            "claim_quote": "05/27/2020",
            "evidence_quote": "April 6, 2022",
            "fact_named": "Mortgage origination date",
        },
    }
    assert finding_stable_key(a) == finding_stable_key(b)
    deduped = dedupe_gotchas([a, b])
    assert len(deduped) == 1
