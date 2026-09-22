"""Grounded documentary fact graph — same nameable fact, not a lie taxonomy.

Extracts entity-scoped attributes (person/employer + attribute + value + quote)
so novel documentary contradictions can conflict without a new hardcoded lie type
per story. Regex/bootstrap extractors name *attributes*; the consistency engine
decides when two values cannot both be true.
"""

from __future__ import annotations

import re
from typing import Any

from dateutil import parser as dateparser

from app.fact_extract import DATE_TOKEN, _date

# Documentary attributes eligible for same-entity conflict (credibility lane).
# These are extractable fields — not a catalog of fraud motifs.
GRAPH_ATTRIBUTES: dict[str, dict[str, Any]] = {
    "employment_start": {
        "label": "Employment start",
        "kind": "date",
        "documentary": True,
    },
    "employment_tax_year": {
        "label": "Employment tax year (W-2)",
        "kind": "year",
        "documentary": True,
    },
}

# Attribute keys that participate in gotcha-style case conflict aggregation.
GRAPH_CONFLICT_KEYS = frozenset(GRAPH_ATTRIBUTES.keys())

MONTHS = (
    r"January|February|March|April|May|June|July|August|September|October|November|December"
)

# "Employed since March 2022" / "employed since March 15, 2022"
EMPLOYED_SINCE_RE = re.compile(
    rf"(?i)(?:employed\s+since|date\s+of\s+hire|hire\s+date|start\s+date|"
    rf"commenced\s+(?:employment|work)|began\s+(?:employment|work))"
    rf"\D{{0,24}}({DATE_TOKEN})"
)

# "in the position of Senior Business Analyst since March 2019"
POSITION_SINCE_RE = re.compile(
    rf"(?i)(?:in\s+the\s+position\s+of|as\s+(?:a|an)\s+)"
    rf"([A-Za-z][\w .&'-]{{1,60}}?)"
    rf"\s+since\s+({DATE_TOKEN})"
)

# "has been employed by TechCore Solutions ... since March 2019"
EMPLOYED_BY_SINCE_RE = re.compile(
    rf"(?i)(?:has\s+been\s+)?employed\s+by\s+"
    rf"([A-Z][A-Za-z0-9 &.'-]{{2,60}}?)"
    rf"(?:\.|,|\s+located|\s+in\b).{{0,100}}?"
    rf"since\s+({DATE_TOKEN})"
)

# "Michael Summers: … Employed since …" or name near employment language
PERSON_NEAR_RE = re.compile(
    r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})\b"
    r"(?:\s*[:,]\s*|\s+(?:is|has\s+been|was)\s+)"
)

PERSON_STOP = frozenset(
    {
        "employment",
        "verification",
        "commitment",
        "mortgage",
        "corporation",
        "issue",
        "date",
        "letter",
        "borrowers",
        "summit",
        "loan",
        "officer",
        "annual",
        "salary",
        "current",
        "senior",
        "business",
        "analyst",
        "registered",
        "nurse",
        "form",
        "statement",
        "employee",
        "employer",
        "company",
        "solutions",
        "medical",
        "center",
        "new",
        "jersey",
        "cherry",
        "hill",
    }
)

EMPLOYER_NEAR_RE = re.compile(
    r"(?:"
    r"\b(?:[Aa]t|[Ww]ith|[Ff]or)\s+"
    r"(?P<preposition>[A-Z][A-Za-z0-9 &.'-]{1,50}?"
    r"(?:Inc|LLC|LLP|Corp|Corporation|Solutions|Systems|Center|Company|Hospital)\.?)"
    r"|"
    r"\b(?P<title>[A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9&.']+){0,3}\s+"
    r"(?:Solutions|Systems|Corporation|Company|Center|Hospital|Inc|LLC))\b"
    r")"
)

EMPLOYEE_LABEL_RE = re.compile(
    r"(?i)\b(?:employee(?:'s)?\s+name|employee|borrower|applicant)\s*[:#]\s*"
    r"([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})"
)

EMPLOYER_LABEL_RE = re.compile(
    r"(?i)\b(?:employer(?:'s)?\s+name|employer)\s*[:#]\s*"
    r"([A-Z][A-Za-z0-9 &.'-]{2,70})"
)

EMPLOYER_NOISE = re.compile(
    r"(?i)\b(employed|employment|position|salary|located|since|borrower|verification)\b"
)

# W-2 tax year: OCR spans newlines; use DOTALL-style [\s\S].
W2_YEAR_RE = re.compile(
    r"(?i)(?:form\s*w-?2|wage\s+and\s+tax\s+statement)[\s\S]{0,400}?"
    r"(?:(?:tax\s+)?year|for\s+(?:the\s+)?year|calendar\s+year)?\D{0,20}"
    r"(20\d{2})"
)

W2_YEAR_ALT_RE = re.compile(
    r"(?i)(?:tax\s+year|for\s+(?:the\s+)?(?:calendar\s+)?year)\s*(20\d{2})"
    r"[\s\S]{0,200}?(?:form\s*w-?2|wage\s+and\s+tax)"
)

# "Form W-2" present in doc + a clear tax year elsewhere on the page
W2_DOC_MARKER_RE = re.compile(r"(?i)\b(?:form\s*w-?2|wage\s+and\s+tax\s+statement)\b")
TAX_YEAR_ONLY_RE = re.compile(
    r"(?i)(?:tax\s+year|calendar\s+year|for\s+(?:the\s+)?year)\s*[:#]?\s*(20\d{2})"
)

# Paystub hire after a W-2 year is impossible for continuous employment at that employer.
PAYSTUB_HIRE_RE = re.compile(
    rf"(?i)(?:hire\s+date|date\s+of\s+hire|start\s+date|employee\s+start)"
    rf"\D{{0,20}}({DATE_TOKEN}|({MONTHS})\.?\s+\d{{4}}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})"
)


def extract_grounded_facts(text: str, limit: int = 40) -> list[dict[str, Any]]:
    """Pull entity-scoped documentary attributes from OCR text."""
    body = text or ""
    if not body.strip():
        return []
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str, str]] = set()

    def _add(rec: dict[str, Any]) -> bool:
        entity = _entity_key(rec)
        token = (rec["key"], entity, str(rec.get("value")))
        if token in seen:
            return False
        seen.add(token)
        facts.append(rec)
        return len(facts) >= limit

    for match in EMPLOYED_SINCE_RE.finditer(body):
        parsed = _date(match.group(1))
        if not parsed:
            continue
        before = body[max(0, match.start() - 320) : match.start()]
        after = body[match.end() : match.end() + 160]
        clause = body[max(0, match.start() - 140) : match.start()]
        person = _nearest_person(clause) or _nearest_person(before)
        # Prefer after-window ("at TechCore…") over clause, which may start mid-word.
        employer = (
            _nearest_employer(after)
            or _nearest_employer(before)
            or _nearest_employer(clause)
        )
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for match in POSITION_SINCE_RE.finditer(body):
        parsed = _date(match.group(2))
        if not parsed:
            continue
        before = body[max(0, match.start() - 320) : match.start()]
        after = body[match.end() : match.end() + 120]
        person = _nearest_person(before)
        employer = (
            _nearest_employer(before)
            or _nearest_employer(after)
            or _nearest_employer(body[max(0, match.start() - 120) : match.end()])
        )
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for match in EMPLOYED_BY_SINCE_RE.finditer(body):
        parsed = _date(match.group(2))
        if not parsed:
            continue
        employer = match.group(1).strip().rstrip(".,")
        before = body[max(0, match.start() - 200) : match.start()]
        person = _nearest_person(before)
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for match in PAYSTUB_HIRE_RE.finditer(body):
        raw = match.group(1)
        parsed = _date(raw)
        if not parsed:
            continue
        window = body[max(0, match.start() - 220) : match.end() + 120]
        # Prefer paystub context
        if not re.search(r"(?i)\b(pay\s*stub|earnings\s+statement|hire\s+date|employee\s+id)\b", window):
            # Still allow if explicit hire date phrase matched
            if not re.search(r"(?i)hire|start\s+date", match.group(0)):
                continue
        person = _nearest_person(window)
        employer = _nearest_employer(window)
        rec = _graph_record(
            "employment_start",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            employer=employer,
            extra=parsed,
        )
        if _add(rec):
            return facts

    for pattern in (W2_YEAR_RE, W2_YEAR_ALT_RE):
        for match in pattern.finditer(body):
            year = int(match.group(1))
            if year < 1990 or year > 2100:
                continue
            window = body[max(0, match.start() - 120) : match.end() + 200]
            person = _nearest_person(window)
            employer = _nearest_employer(window)
            rec = _graph_record(
                "employment_tax_year",
                year,
                str(year),
                match.group(0)[:220],
                person=person,
                employer=employer,
            )
            if _add(rec):
                return facts

    # Fallback: W-2 marker anywhere + explicit tax year on the page
    if W2_DOC_MARKER_RE.search(body):
        for match in TAX_YEAR_ONLY_RE.finditer(body):
            year = int(match.group(1))
            if year < 1990 or year > 2100:
                continue
            window = body[max(0, match.start() - 200) : match.end() + 200]
            person = _nearest_person(window) or _nearest_person(body[:400])
            employer = _nearest_employer(window) or _nearest_employer(body[:500])
            rec = _graph_record(
                "employment_tax_year",
                year,
                str(year),
                match.group(0)[:220],
                person=person,
                employer=employer,
            )
            if _add(rec):
                return facts

    return facts


def cluster_key(fact: dict[str, Any]) -> str:
    """Same nameable fact identity for conflict clustering."""
    key = str(fact.get("key") or "")
    entity = _entity_key(fact)
    if entity and entity != "_":
        return f"{key}::{entity}"
    return key


def _entity_key(fact: dict[str, Any]) -> str:
    person = _norm_name(str(fact.get("person") or ""))
    employer = _norm_name(str(fact.get("employer") or ""))
    if person and employer:
        return f"{person}|{employer}"
    if person:
        return person
    if employer:
        return f"|{employer}"
    return "_"


def entity_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """True when both facts can describe the same person/employer (soft match).

    Person-only and person|employer records for the same person merge; conflicting
    employers or persons do not. Unscoped facts only merge with other unscoped facts.
    """
    pa = _norm_name(str(left.get("person") or ""))
    pb = _norm_name(str(right.get("person") or ""))
    ea = _norm_name(str(left.get("employer") or ""))
    eb = _norm_name(str(right.get("employer") or ""))
    if pa and pb and pa != pb:
        return False
    if ea and eb and ea != eb:
        return False
    if pa and pb and pa == pb:
        return True
    if ea and eb and ea == eb:
        return True
    if not pa and not pb and not ea and not eb:
        return True
    # One side fully unscoped — do not glue onto a named entity (too noisy).
    return False


def same_nameable_fact(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Same attribute (or hire-vs-W2 cross-attribute) for a compatible entity."""
    keys = {left.get("key"), right.get("key")}
    if left.get("key") == right.get("key"):
        return entity_compatible(left, right)
    if keys == {"employment_start", "employment_tax_year"}:
        return entity_compatible(left, right)
    return False


def facts_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    """Return False when two grounded facts for the same cluster cannot both be true."""
    if not same_nameable_fact(left, right):
        # Strict cluster_key equality still used for non-graph / exact keys
        if cluster_key(left) != cluster_key(right):
            return True
        if left.get("key") != right.get("key"):
            return True
    key = left.get("key")
    if key != right.get("key"):
        # Cross-attribute documentary rule: W-2 tax year vs later hire date
        if {left.get("key"), right.get("key")} == {"employment_start", "employment_tax_year"}:
            return not _hire_after_w2_year(left, right)
        return True
    if key == "employment_start":
        return _same_date_value(left, right)
    if key == "employment_tax_year":
        return str(left.get("value")) == str(right.get("value"))
    return str(left.get("value")) == str(right.get("value"))


def _hire_after_w2_year(a: dict[str, Any], b: dict[str, Any]) -> bool:
    start = a if a.get("key") == "employment_start" else b
    tax = b if a.get("key") == "employment_start" else a
    try:
        year = int(tax.get("value"))
    except (TypeError, ValueError):
        return False
    start_year = start.get("year")
    if start_year is None:
        try:
            start_year = int(str(start.get("value"))[:4])
        except (TypeError, ValueError):
            return False
    # A W-2 for year Y asserts wages that year; hire date after Y cannot both be true
    # for continuous employment at that employer.
    return int(start_year) > year


def _same_date_value(a: dict[str, Any], b: dict[str, Any]) -> bool:
    if a.get("value") == b.get("value"):
        return True
    # Same month-key with different day is soft-compatible for non-deadline dates
    if a.get("month_key") and a.get("month_key") == b.get("month_key"):
        return True
    return False


def _graph_record(
    key: str,
    value: Any,
    display: str,
    quote: str,
    *,
    person: str | None = None,
    employer: str | None = None,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    meta = GRAPH_ATTRIBUTES[key]
    rec = {
        "key": key,
        "label": meta["label"],
        "kind": meta["kind"],
        "value": value,
        "display": display,
        "quote": re.sub(r"\s+", " ", quote).strip()[:220],
        "documentary": True,
        "graph": True,
        "person": person,
        "employer": employer,
        "entity_key": None,
    }
    if extra:
        rec.update({k: v for k, v in extra.items() if k not in rec})
    rec["entity_key"] = _entity_key(rec)
    return rec


def _nearest_person(window: str) -> str | None:
    candidates: list[str] = []
    for match in EMPLOYEE_LABEL_RE.finditer(window or ""):
        candidates.append(match.group(1).strip())
    for match in re.finditer(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})\s*:",
        window or "",
    ):
        candidates.append(match.group(1).strip())
    for match in PERSON_NEAR_RE.finditer(window or ""):
        candidates.append(match.group(1).strip())
    # Fallback: Title Case pairs
    candidates.extend(re.findall(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b", window or ""))

    def _ok(name: str) -> bool:
        parts = name.split()
        if len(parts) < 2:
            return False
        return not any(p.lower().strip(".") in PERSON_STOP for p in parts)

    good = [c for c in candidates if _ok(c)]
    if good:
        return good[-1]
    return None


def _employer_ok(name: str) -> bool:
    n = (name or "").strip().rstrip(".,")
    if len(n) < 3 or len(n) > 80:
        return False
    if EMPLOYER_NOISE.search(n):
        return False
    # Reject mid-word window chops like "oyed by TechCore Solutions"
    if n[:1].islower():
        return False
    return True


def _clean_employer(name: str) -> str:
    return (name or "").strip().rstrip(".,")


def _nearest_employer(window: str) -> str | None:
    label = EMPLOYER_LABEL_RE.search(window or "")
    if label:
        cand = _clean_employer(label.group(1))
        if _employer_ok(cand):
            return cand
    match = EMPLOYER_NEAR_RE.search(window or "")
    if match:
        cand = _clean_employer(match.group("preposition") or match.group("title") or "")
        if _employer_ok(cand):
            return cand
    loose = re.search(
        r"\b([A-Z][A-Za-z0-9]+(?:\s+[A-Z][A-Za-z0-9]+){0,3}\s+"
        r"(?:Solutions|Systems|Corporation|Company|Center|Hospital|Inc|LLC))\b",
        window or "",
    )
    if loose:
        cand = _clean_employer(loose.group(1))
        if _employer_ok(cand):
            return cand
    return None


def _norm_name(value: str) -> str:
    n = re.sub(r"[.,]", "", value or "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    n = re.sub(
        r"\b(inc|llc|llp|corp|corporation|company|co|the)\b",
        "",
        n,
    )
    return re.sub(r"\s+", " ", n).strip()
