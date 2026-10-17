"""Case Impact hygiene: no proxy-doc flood, index warn, simultaneous-service gate."""

from __future__ import annotations

from app.auto_title import (
    index_mismatch_warning,
    normalize_index_number,
    parse_index_from_filename,
)
from app.case_consistency import (
    aggregate_fact_conflicts,
    compare_to_case,
    dedupe_gotchas,
    finding_stable_key,
)
from app.fact_graph import (
    facts_compatible,
    _service_attempt_record,
)
from app.report.scoring import score_risk


def _service(
    method: str,
    time_s: str,
    place: str = "1847 Bedford Avenue, Brooklyn, New York",
    *,
    server: str = "Timothy Jennings",
    on_date: str = "2019-04-12",
    doc_id: str | None = "aos-1",
) -> dict:
    rec = _service_attempt_record(
        server,
        on_date,
        "April 12, 2019",
        time_s,
        place,
        method,
        f"On April 12, 2019 at {time_s} I performed {method} service at {place}.",
    )
    if doc_id:
        rec["document_id"] = doc_id
    return rec


def test_hours_apart_personal_substitute_same_aos_suppressed():
    """Ordinary personal-then-substitute same day is advocacy — must_suppress."""
    personal = _service("personal", "10:15 a.m.")
    substitute = _service("substitute", "3:45 p.m.")
    assert facts_compatible(personal, substitute)


def test_same_minute_personal_substitute_same_aos_ships():
    """True simultaneous personal + substitute with quotes still interrupts."""
    personal = _service("personal", "10:15 a.m.")
    substitute = _service("substitute", "10:15 a.m.")
    assert not facts_compatible(personal, substitute)


def test_cross_doc_aos_vs_log_still_ships():
    """AOS personal vs separate daily-log nail-mail remains AGAINST."""
    aos = _service("personal", "2:30 p.m.", doc_id="aos")
    log = _service("nail_mail", "9:47 a.m.", doc_id="daily-log")
    assert not facts_compatible(aos, log)


def test_occupancy_conflict_not_attached_to_later_clean_affidavit():
    """Matter-level once: later clean affidavit must not get occupancy fact_conflict."""
    occ_a = {
        "key": "occupancy_start",
        "kind": "date",
        "value": "2019-04-03",
        "display": "April 3, 2019",
        "quote": (
            "I have occupied the Subject Property continuously as my primary "
            "residence since April 3, 2019."
        ),
        "documentary": True,
        "graph": True,
        "person": "Wayne Alexander",
    }
    occ_b = {
        "key": "occupancy_start",
        "kind": "date",
        "value": "2020-05-17",
        "display": "May 17, 2020",
        "quote": (
            "I have occupied the Subject Property continuously as my primary "
            "residence since May 17, 2020."
        ),
        "documentary": True,
        "graph": True,
        "person": "Wayne Alexander",
    }
    clean_affidavit_facts = [
        {
            "key": "execution_venue",
            "kind": "text",
            "value": "kings",
            "display": "Kings County",
            "quote": "Sworn to before me in Kings County",
            "documentary": True,
            "graph": True,
        }
    ]
    # Real conflict between two occupancy affidavits → aggregate ships; pairwise
    # on a later clean filing must not paint that filing red.
    snaps = [
        {
            "document_id": "occ-a",
            "display_name": "Occupancy Affidavit A",
            "doc_kind": "affidavit",
            "facts": [occ_a],
            "text_excerpt": occ_a["quote"],
        },
        {
            "document_id": "occ-b",
            "display_name": "Occupancy Affidavit B",
            "doc_kind": "affidavit",
            "facts": [occ_b],
            "text_excerpt": occ_b["quote"],
        },
        {
            "document_id": "clean-aff",
            "display_name": "Attorney Affirmation",
            "doc_kind": "affirmation",
            "facts": clean_affidavit_facts,
            "text_excerpt": "I am an attorney admitted to practice.",
        },
    ]
    conflicts = [
        c
        for c in aggregate_fact_conflicts(snaps)
        if c.get("field") == "occupancy_start"
        and str(c.get("severity") or "") in {"high", "medium", "critical"}
    ]
    assert conflicts, "occupancy conflict must still ship at matter level"
    related_ids = {
        str(d.get("document_id"))
        for c in conflicts
        for d in (c.get("related_documents") or [])
    }
    assert "clean-aff" not in related_ids

    findings, matrix = compare_to_case(
        new_document_id="clean-aff",
        new_display_name="Attorney Affirmation",
        new_entities={},
        new_amounts=[],
        new_fingerprint="fp-clean",
        prior_snapshots=snaps[:2],
        new_doc_kind="affirmation",
        new_facts=clean_affidavit_facts,
        new_text="I am an attorney admitted to practice.",
        case_documents=[],
    )
    occ_findings = [
        f
        for f in findings
        if (f.evidence or {}).get("field") == "occupancy_start"
        or "occupancy" in (f.title or "").lower()
    ]
    assert not occ_findings
    risk, score = score_risk(findings)
    assert risk == "green" and score < 35


def test_billing_as_occupancy_does_not_flood_clean_affidavits():
    """Billing-period mislabel must not create an occupancy conflict card."""
    billing = {
        "key": "occupancy_start",
        "kind": "date",
        "value": "2009-06-02",
        "display": "06/02/09",
        "quote": (
            "Billing Period: 06/02/09 to 07/02/09 7972 FISHER ISLAND DR "
            "UNIT 7972 MIAMI BEACH, FL"
        ),
        "documentary": True,
        "graph": True,
        "person": "Wayne Alexander",
    }
    mort = {
        "key": "mortgage_origination",
        "kind": "date",
        "value": "2006-08-01",
        "display": "August 1, 2006",
        "quote": "Note signed by Borrower and dated August 01, 2006",
        "documentary": True,
        "graph": True,
        "date_role": "origination",
        "instrument_event": "note",
    }
    snaps = [
        {
            "document_id": "bill",
            "display_name": "Utility bill",
            "doc_kind": "bill",
            "facts": [billing],
            "text_excerpt": billing["quote"],
        },
        {
            "document_id": "note",
            "display_name": "Note",
            "doc_kind": "exhibit",
            "facts": [mort],
            "text_excerpt": mort["quote"],
        },
        {
            "document_id": "aff",
            "display_name": "Affidavit of Merit",
            "doc_kind": "affidavit",
            "facts": [],
            "text_excerpt": "I am familiar with the books and records.",
        },
    ]
    conflicts = [
        c
        for c in aggregate_fact_conflicts(snaps)
        if str(c.get("severity") or "") in {"high", "medium", "critical"}
    ]
    assert not any(
        c.get("field") in {"occupancy_start", "mortgage_origination"} for c in conflicts
    )


def test_forgery_cards_dedupe_matter_level():
    """Same forged-exhibit signal on two scans → one Case Impact card."""
    a = {
        "id": "f1",
        "document_id": "s41",
        "title": "Statement date conflict",
        "tainted_support": True,
        "evidence": {
            "analysis_type": "date_inconsistency",
            "quoted_evidence": "due by December 29, 2011 vs APR. 29, 2007",
        },
    }
    b = {
        "id": "f2",
        "document_id": "s41-rescan",
        "title": "Statement date conflict",
        "tainted_support": True,
        "evidence": {
            "analysis_type": "date_inconsistency",
            "quoted_evidence": "due by December 29, 2011 vs APR. 29, 2007",
        },
    }
    assert finding_stable_key(a) == finding_stable_key(b)
    deduped = dedupe_gotchas([a, b])
    assert len(deduped) == 1
    related = {d.get("document_id") for d in (deduped[0].get("related_documents") or [])}
    assert "s41" in related
    assert "s41-rescan" in related


def test_parse_index_from_nyscef_filename():
    name = "11467_2007_U_S_Bank_National_Ass_v_U_S_Bank_National_Ass_EXHIBIT_S__41.pdf"
    assert parse_index_from_filename(name) == "11467/2007"
    assert normalize_index_number("11467/2007") == "11467/2007"
    assert normalize_index_number("11467_2007") == "11467/2007"
    warn = index_mismatch_warning(name, "702161/2022")
    assert warn is not None
    assert warn["filename_index"] == "11467/2007"
    assert warn["matter_index"] == "702161/2022"
    assert index_mismatch_warning(name, "11467/2007") is None
    assert index_mismatch_warning("plain_affidavit.pdf", "11467/2007") is None
