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

# Documentary attributes eligible for same-entity conflict.
# Attribute names only — the attorney interrupt is credibility / element when
# the party's own papers cannot both be true.
GRAPH_ATTRIBUTES: dict[str, dict[str, Any]] = {
    "employment_start": {
        "label": "Employment start",
        "kind": "date",
        "documentary": True,
        "impact": "credibility",
    },
    "employment_tax_year": {
        "label": "Employment tax year (W-2)",
        "kind": "year",
        "documentary": True,
        "impact": "credibility",
    },
    "date_of_birth": {
        "label": "Date of birth",
        "kind": "date",
        "documentary": True,
        "impact": "credibility",
        "deadline": True,
    },
    "residence_address": {
        "label": "Residence / occupancy address",
        "kind": "address",
        "documentary": True,
        "impact": "element",
    },
    "annual_income": {
        "label": "Annual income (stated or tax)",
        "kind": "money",
        "documentary": True,
        "impact": "credibility",
        "abs": 8000.0,
        "pct": 0.20,
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
        "maple",
        "avenue",
        "market",
        "street",
        "francisco",
        "san",
        "queens",
        "brooklyn",
        "manhattan",
        "adjusted",
        "gross",
        "income",
        "primary",
        "residence",
        "driver",
        "license",
        "uniform",
        "residential",
        "application",
        "deposition",
        "transcript",
        "continuously",
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
    r"(?i)\b(?:employee(?:'s)?\s+name|employee|borrower|applicant|"
    r"deponent|taxpayer|name)\s*[:#]\s*"
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

# Person identity / occupancy / income — attorney-winning documentary attributes.
DOB_RE = re.compile(
    rf"(?i)(?:date\s+of\s+birth|d\.?o\.?b\.?|birth\s+date|born\s+on)"
    rf"\s*[:#]?\s*({DATE_TOKEN}|\d{{1,2}}[/-]\d{{1,2}}[/-]\d{{2,4}})"
)

RESIDENCE_RE = re.compile(
    r"(?i)(?:primary\s+residence|resides?\s+at|residential\s+address|"
    r"home\s+address|dwelling\s+(?:at|address)|I\s+live\s+at|"
    r"continuously\s+(?:resided|lived)\s+at|"
    r"(?:driver'?s?\s+)?licen[cs]e\s+address|mailing\s+address\s+on\s+(?:tax|return)|"
    r"(?:tax\s+return|form\s*1040).{0,40}address)"
    r"\s*[:#]?\s*"
    r"([0-9]{1,6}\s+[A-Za-z0-9 .'-]{2,60}(?:,?\s*[A-Za-z .'-]{2,40}){0,3})"
)

# Bare "220 Maple … Queens" after primary-residence language without "at"
RESIDENCE_INLINE_RE = re.compile(
    r"(?i)primary\s+residence\s+"
    r"([0-9]{1,6}\s+[A-Za-z0-9 .'-]{2,80})"
)

INCOME_MONTHLY_RE = re.compile(
    r"(?i)(?:gross\s+monthly(?:\s+income)?|monthly\s+(?:gross\s+)?income|"
    r"base\s+monthly\s+(?:income|salary))"
    r"[^\d$]{0,24}\$?\s*([\d,]{2,12}(?:\.\d{2})?)"
)

INCOME_ANNUAL_RE = re.compile(
    r"(?i)(?:(?:adjusted\s+gross\s+income|\bAGI\b)|"
    r"annual\s+(?:gross\s+)?(?:income|salary)|gross\s+annual(?:\s+income)?|"
    r"yearly\s+(?:gross\s+)?income)"
    r"[^\d$]{0,24}\$?\s*([\d,]{2,12}(?:\.\d{2})?)"
)

INCOME_APPROX_ANNUAL_RE = re.compile(
    r"(?i)(?:gross\s+monthly|monthly\s+income)[^\d$]{0,40}"
    r"\$?\s*([\d,]{2,12}(?:\.\d{2})?)"
    r"[^\n]{0,40}(?:≈|~|approximately|about|roughly)?\s*"
    r"(?:\$?\s*([\d,]{2,12})\s*k\b|\$?\s*([\d,]{2,12}(?:\.\d{2})?)\s*(?:per\s+year|/yr|annual))"
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

    # --- Person documentary attributes (identity / occupancy / income) ---
    for match in DOB_RE.finditer(body):
        parsed = _date(match.group(1))
        if not parsed:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 80]
        person = _nearest_person(window) or _nearest_person(body[:300])
        rec = _graph_record(
            "date_of_birth",
            parsed["value"],
            parsed["display"],
            match.group(0),
            person=person,
            extra={**parsed, "deadline": True},
        )
        if _add(rec):
            return facts

    for pattern in (RESIDENCE_RE, RESIDENCE_INLINE_RE):
        for match in pattern.finditer(body):
            raw_addr = match.group(1).strip().rstrip(".,;")
            raw_addr = re.split(r"(?i)\b(?:continuously|since|for\s+\d+\s+years?)\b", raw_addr)[0].strip(" ,.")
            norm = _norm_address(raw_addr)
            if not norm or len(norm) < 6:
                continue
            window = body[max(0, match.start() - 220) : match.end() + 80]
            # Prefer person labeled earlier in the document when window is address-heavy
            person = (
                _nearest_person(window)
                or _nearest_person(body[: match.start()])
                or _nearest_person(body[:400])
            )
            rec = _graph_record(
                "residence_address",
                norm,
                raw_addr[:120],
                match.group(0)[:220],
                person=person,
            )
            if _add(rec):
                return facts

    for match in INCOME_APPROX_ANNUAL_RE.finditer(body):
        monthly = _parse_money(match.group(1))
        annual = None
        if match.group(2):
            annual = _parse_money(match.group(2))
            if annual is not None and annual < 1000:
                annual = annual * 1000.0  # "$90k"
        elif match.group(3):
            annual = _parse_money(match.group(3))
        if annual is None and monthly is not None:
            annual = monthly * 12.0
        if annual is None:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = (
            _nearest_person(window)
            or _nearest_person(body[: match.start()])
            or _nearest_person(body[:300])
        )
        rec = _graph_record(
            "annual_income",
            round(annual, 2),
            f"${annual:,.0f}/yr",
            match.group(0)[:220],
            person=person,
            extra={"income_source": "monthly_stated"},
        )
        if _add(rec):
            return facts

    for match in INCOME_MONTHLY_RE.finditer(body):
        monthly = _parse_money(match.group(1))
        if monthly is None or monthly < 100:
            continue
        annual = monthly * 12.0
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = (
            _nearest_person(window)
            or _nearest_person(body[: match.start()])
            or _nearest_person(body[:300])
        )
        rec = _graph_record(
            "annual_income",
            round(annual, 2),
            f"${monthly:,.0f}/mo (~${annual:,.0f}/yr)",
            match.group(0)[:220],
            person=person,
            extra={"income_source": "monthly", "monthly": monthly},
        )
        if _add(rec):
            return facts

    for match in INCOME_ANNUAL_RE.finditer(body):
        annual = _parse_money(match.group(1))
        if annual is None or annual < 1000:
            continue
        window = body[max(0, match.start() - 200) : match.end() + 40]
        person = (
            _nearest_person(window)
            or _nearest_person(body[: match.start()])
            or _nearest_person(body[:300])
        )
        source = "agi" if re.search(r"(?i)\bAGI\b|adjusted\s+gross", match.group(0)) else "annual"
        rec = _graph_record(
            "annual_income",
            round(annual, 2),
            f"${annual:,.0f}/yr",
            match.group(0)[:220],
            person=person,
            extra={"income_source": source},
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
    if key == "date_of_birth":
        # Any day/year mismatch is identity BS — not soft same-month.
        return str(left.get("value")) == str(right.get("value"))
    if key == "employment_tax_year":
        return str(left.get("value")) == str(right.get("value"))
    if key == "residence_address":
        return _addresses_compatible(left.get("value"), right.get("value"))
    if key == "annual_income":
        return _income_compatible(left, right)
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


def _addresses_compatible(left: Any, right: Any) -> bool:
    a = _norm_address(str(left or ""))
    b = _norm_address(str(right or ""))
    if not a or not b:
        return True
    if a == b:
        return True
    # Same street number + street token → compatible even if city OCR differs slightly
    a_parts = a.split()
    b_parts = b.split()
    if len(a_parts) >= 2 and len(b_parts) >= 2 and a_parts[0] == b_parts[0] and a_parts[1] == b_parts[1]:
        # Different city/state after the street → conflict
        if len(a_parts) >= 3 and len(b_parts) >= 3 and a_parts[2:] != b_parts[2:]:
            # Queens vs San Francisco style — conflict when remaining tokens disjoint
            return bool(set(a_parts[2:]) & set(b_parts[2:]))
        return True
    return False


def _income_compatible(left: dict[str, Any], right: dict[str, Any]) -> bool:
    try:
        a = float(left.get("value"))
        b = float(right.get("value"))
    except (TypeError, ValueError):
        return True
    delta = abs(a - b)
    baseline = max(a, b, 1.0)
    meta = GRAPH_ATTRIBUTES.get("annual_income") or {}
    return not (delta > float(meta.get("abs") or 8000) or (delta / baseline) > float(meta.get("pct") or 0.20))


def _parse_money(raw: str | None) -> float | None:
    if not raw:
        return None
    cleaned = re.sub(r"[^\d.]", "", str(raw))
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _norm_address(value: str) -> str:
    n = re.sub(r"[#,]", " ", value or "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    n = re.sub(
        r"\b(street|st|avenue|ave|boulevard|blvd|road|rd|drive|dr|lane|ln|court|ct|"
        r"place|pl|apartment|apt|unit|suite|ste|continuously|since)\b\.?",
        "",
        n,
    )
    n = re.sub(r"\s+", " ", n).strip()
    # Drop trailing years that rode along ("since 2018")
    n = re.sub(r"\b(19|20)\d{2}\b", "", n).strip()
    return re.sub(r"\s+", " ", n).strip()


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
    def _ok(name: str) -> bool:
        parts = name.split()
        if len(parts) < 2:
            return False
        return not any(p.lower().strip(".") in PERSON_STOP for p in parts)

    labeled: list[str] = []
    for match in EMPLOYEE_LABEL_RE.finditer(window or ""):
        labeled.append(match.group(1).strip())
    for match in re.finditer(
        r"\b([A-Z][a-z]+(?:\s+[A-Z][a-z'.]+){1,3})\s*:",
        window or "",
    ):
        labeled.append(match.group(1).strip())
    for match in PERSON_NEAR_RE.finditer(window or ""):
        labeled.append(match.group(1).strip())

    good_labeled = [c for c in labeled if _ok(c)]
    if good_labeled:
        return good_labeled[-1]

    # Fallback Title Case pairs — last resort only
    loose = re.findall(r"\b([A-Z][a-z]+\s+[A-Z][a-z]+)\b", window or "")
    good_loose = [c for c in loose if _ok(c)]
    if good_loose:
        return good_loose[0]
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
