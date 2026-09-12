"""Classify case documents as real evidence vs advocacy.

Gotcha Finder may only use a quote as the counter-statement when it is Tier 1
(sworn, documentary, or an entered court act). Two unsworn motion narratives
disagreeing is litigation, not a gotcha.
"""

from __future__ import annotations

import calendar
import re
from datetime import date
from typing import Any

TIER_EVIDENCE = 1
TIER_ADVOCACY = 2

TIER_LABELS = {
    TIER_EVIDENCE: "evidence",
    TIER_ADVOCACY: "advocacy",
}

EVIDENCE_KINDS = frozenset(
    {
        "exhibit",
        "bill",
        "statement",
        "invoice",
        "check",
        "receipt",
        "photo",
        "image",
        "evidence",
        "proof_of_service",
        "transcript",
        "judgment",
    }
)

ADVOCACY_KINDS = frozenset(
    {
        "motion",
        "complaint",
        "answer",
        "brief",
        "memorandum",
        "reply",
        "opposition",
        "letter",
        "notice",
        "affirmation",
        "rji",
        "calendar",
        "notice_of_pendency",
        "filing",
    }
)

PROPOSED_ACT = re.compile(
    r"(?i)\b("
    r"proposed\s+(?:order|judgment|decision|findings?)|"
    r"settle(?:ment)?\s+(?:this\s+)?order|"
    r"counter-?order|"
    r"please\s+take\s+notice\s+that.{0,80}proposed"
    r")\b"
)
ENTERED_ORDER = re.compile(
    r"(?i)\b("
    r"so-?ordered|"
    r"enter(?:ed|ing)\s+(?:this\s+)?(?:order|judgment)|"
    r"it\s+is\s+(?:hereby\s+)?ordered|"
    r"the\s+clerk\s+(?:is\s+)?directed|"
    r"e-?filed\s+order|"
    r"decision\s+and\s+order"
    r")\b"
)
SWORN_RECORD = re.compile(
    r"(?i)\b("
    r"affidavit\s+of\s+service|"
    r"sworn\s+(?:to|before)|"
    r"duly\s+sworn|"
    r"being\s+duly\s+sworn|"
    r"notary|"
    r"subscribed\s+and\s+sworn|"
    r"under\s+penalty\s+of\s+perjury|"
    r"business\s+records?|"
    r"certified\s+copy|"
    r"official\s+record"
    r")\b"
)
SERVICE_RECORD = re.compile(r"(?i)\baffidavit\s+of\s+service|proof\s+of\s+service|process\s+server\b")
WEEKDAY_COMMA_DATE = re.compile(
    r"(?i)\b(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)"
    r",?\s+(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4})\b"
)
DATE_A_WEEKDAY = re.compile(
    r"(?i)\b(January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\s+(\d{1,2}),?\s+(\d{4}),?\s+(?:a\s+|on\s+)?(Monday|Tuesday|Wednesday|Thursday|Friday|Saturday|Sunday)\b"
)

_MONTHS = {
    "january": 1,
    "february": 2,
    "march": 3,
    "april": 4,
    "may": 5,
    "june": 6,
    "july": 7,
    "august": 8,
    "september": 9,
    "october": 10,
    "november": 11,
    "december": 12,
}
_WEEKDAYS = {
    "monday": 0,
    "tuesday": 1,
    "wednesday": 2,
    "thursday": 3,
    "friday": 4,
    "saturday": 5,
    "sunday": 6,
}


def classify_tier(
    doc: dict[str, Any] | None = None,
    quote: str = "",
    source_name: str = "",
    doc_kind: str | None = None,
) -> int:
    """Return 1 (evidence) or 2 (advocacy). Ambiguous advocacy wins."""
    kind = (doc_kind or (doc or {}).get("doc_kind") or "").strip().lower()
    name = " ".join(
        [
            source_name,
            (doc or {}).get("display_name") or "",
            (doc or {}).get("original_filename") or "",
        ]
    )
    blob = f"{name}\n{quote}"

    if PROPOSED_ACT.search(blob) or re.search(r"(?i)\bproposed\b", name):
        return TIER_ADVOCACY
    if SERVICE_RECORD.search(name) or (kind == "affidavit" and SERVICE_RECORD.search(blob)):
        return TIER_EVIDENCE
    if kind in EVIDENCE_KINDS:
        return TIER_EVIDENCE
    if kind == "stipulation":
        return TIER_EVIDENCE
    if kind == "order":
        if PROPOSED_ACT.search(blob) or re.search(r"(?i)\bproposed\b", name):
            return TIER_ADVOCACY
        if ENTERED_ORDER.search(blob) or re.search(r"(?i)\b(decision\s+and\s+order|so-?ordered)\b", name):
            return TIER_EVIDENCE
        return TIER_ADVOCACY
    if kind == "affidavit":
        return TIER_EVIDENCE
    if kind in ADVOCACY_KINDS:
        return TIER_ADVOCACY
    if re.search(r"(?i)\bexhibit\b", name):
        return TIER_EVIDENCE
    if re.search(r"(?i)\b(motion|memorandum|proposed\s+order|opposition|reply\s+affirmation)\b", name):
        return TIER_ADVOCACY
    if SWORN_RECORD.search(blob) or re.search(
        r"(?i)\b(notary|executed|hospital|intake|register\s+video|phone\s+extract|"
        r"school\s+sign-?out|bank\s+letter|treating\s+note|stipulation|"
        r"affidavit|exhibit|email)\b",
        quote,
    ):
        return TIER_EVIDENCE
    return TIER_ADVOCACY


def source_looks_like_advocacy(name: str) -> bool:
    return bool(
        re.search(
            r"(?i)("
            r"^\s*(motion|complaint|answer|memorandum|opposition|reply|proposed)|"
            r"\b(motion\s+to|proposed\s+order|reargue|memorandum\s+of\s+law)\b"
            r")",
            name or "",
        )
    )


def evidence_is_probative(candidate: dict[str, Any], docs: list[dict[str, Any]] | None = None) -> bool:
    """True only when the counter-quote is Tier 1 evidence."""
    if candidate.get("evidence_tier") == TIER_ADVOCACY:
        return False
    if candidate.get("evidence_tier") == TIER_EVIDENCE:
        return True
    source = str(candidate.get("evidence_source") or "")
    if source_looks_like_advocacy(source):
        return False
    doc = _lookup_doc(None, source, docs) or _lookup_doc(candidate.get("document_id"), source, docs)
    return (
        classify_tier(
            doc,
            str(candidate.get("evidence_quote") or ""),
            source,
            candidate.get("evidence_kind") or (doc or {}).get("doc_kind"),
        )
        == TIER_EVIDENCE
    )


def claim_tier(candidate: dict[str, Any], docs: list[dict[str, Any]] | None = None) -> int:
    if candidate.get("claim_tier") in {TIER_EVIDENCE, TIER_ADVOCACY}:
        return int(candidate["claim_tier"])
    doc = _lookup_doc(candidate.get("claim_document_id"), candidate.get("claim_source"), docs)
    return classify_tier(
        doc,
        str(candidate.get("claim_quote") or ""),
        str(candidate.get("claim_source") or ""),
        candidate.get("claim_kind"),
    )


def is_self_undercut(candidate: dict[str, Any]) -> bool:
    """Claim and counter-proof appear to come from the same side or the same file."""
    claim_id = candidate.get("claim_document_id")
    ev_id = candidate.get("document_id")
    if claim_id and ev_id and claim_id == ev_id:
        return True
    claim_src = str(candidate.get("claim_source") or "").lower()
    ev_src = str(candidate.get("evidence_source") or "").lower()
    if claim_src and ev_src and claim_src == ev_src:
        return True
    tag = re.search(r"(?i)\bexhibit\s+[\"']?([A-Z0-9]{1,4})\b", claim_src + " " + str(candidate.get("claim_quote") or ""))
    if tag and re.search(rf"(?i)\bexhibit\s+[\"']?{re.escape(tag.group(1))}\b", ev_src):
        return True
    return False


def counter_label(tier: int) -> str:
    return "Evidence" if tier == TIER_EVIDENCE else "Opposing assertion"


def find_internal_evidence_defects(
    *,
    text: str,
    source: str,
    document_id: str,
    doc_kind: str | None = None,
) -> list[dict[str, Any]]:
    """Tier 1 documents only: dates that name the wrong weekday."""
    if classify_tier({"doc_kind": doc_kind, "display_name": source}, text, source, doc_kind) != TIER_EVIDENCE:
        return []
    out: list[dict[str, Any]] = []
    for stated, actual, quote in _weekday_conflicts(text):
        out.append(
            {
                "title": "Apparent chronology inconsistency",
                "claim_quote": quote,
                "claim_source": source,
                "claim_document_id": document_id,
                "claim_kind": doc_kind,
                "claim_tier": TIER_EVIDENCE,
                "evidence_quote": f"The calendar date in that sentence falls on {actual}, not {stated}.",
                "evidence_source": source,
                "document_id": document_id,
                "evidence_kind": doc_kind,
                "evidence_tier": TIER_EVIDENCE,
                "why_it_may_matter": (
                    "A sworn or documentary record that names the wrong weekday for its own date "
                    "is internally inconsistent and worth verifying before relying on it."
                ),
                "self_undercut": True,
            }
        )
    return out[:3]


def _weekday_conflicts(text: str) -> list[tuple[str, str, str]]:
    found: list[tuple[str, str, str]] = []
    for match in WEEKDAY_COMMA_DATE.finditer(text or ""):
        stated, month, day, year = match.group(1), match.group(2), match.group(3), match.group(4)
        actual = _actual_weekday(month, day, year)
        if actual and actual != stated.lower():
            found.append((stated, actual.title(), match.group(0)))
    for match in DATE_A_WEEKDAY.finditer(text or ""):
        month, day, year, stated = match.group(1), match.group(2), match.group(3), match.group(4)
        actual = _actual_weekday(month, day, year)
        if actual and actual != stated.lower():
            found.append((stated, actual.title(), match.group(0)))
    return found


def _actual_weekday(month: str, day: str, year: str) -> str | None:
    try:
        parsed = date(int(year), _MONTHS[month.lower()], int(day))
    except (KeyError, ValueError):
        return None
    return calendar.day_name[parsed.weekday()].lower()


def _lookup_doc(
    document_id: str | None,
    name: str | None,
    docs: list[dict[str, Any]] | None,
) -> dict[str, Any] | None:
    if not docs:
        return None
    if document_id:
        for doc in docs:
            if doc.get("id") == document_id:
                return doc
    query = (name or "").strip().lower()
    if not query:
        return None
    for doc in docs:
        hay = f"{doc.get('display_name') or ''} {doc.get('original_filename') or ''}".strip().lower()
        if hay == query or query in hay or hay in query:
            return doc
    return None
