"""Pull labeled litigation facts from a filing so the same fact can be compared.

Claude's case-clerk spec: extract dates, amounts, and parties with enough context
to compare loan amount to loan amount — not a bag of every dollar figure on the page.
"""

from __future__ import annotations

import re
from typing import Any

from dateutil import parser as dateparser

MONEY = r"\$?\s*(\d{1,3}(?:,\d{3})+(?:\.\d{2})?|\d{1,3}(?:,\d{3})*(?:\.\d{2}))"
DATE_TOKEN = (
    r"(?:"
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\.?\s+\d{1,2},?\s+\d{4}"
    r"|(?:January|February|March|April|May|June|July|August|September|October|November|December)"
    r"\.?\s+\d{4}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r")"
)

FACT_META: dict[str, dict[str, Any]] = {
    "loan_amount": {
        "label": "Loan amount",
        "kind": "money",
        "abs": 1000.0,
        "pct": 0.005,
    },
    "interest_rate": {
        "label": "Interest rate",
        "kind": "rate",
        "abs": 0.1,
        "pct": 0.0,
    },
    "monthly_payment": {
        "label": "Monthly payment",
        "kind": "money",
        "abs": 50.0,
        "pct": 0.03,
    },
    "outstanding_balance": {
        "label": "Outstanding balance",
        "kind": "money",
        "abs": 5000.0,
        "pct": 0.015,
    },
    "fees": {
        "label": "Fees / accrued interest",
        "kind": "money",
        "abs": 1000.0,
        "pct": 0.10,
    },
    "loan_origination_date": {"label": "Loan origination date", "kind": "date"},
    "note_date": {"label": "Note date", "kind": "date"},
    "mortgage_date": {"label": "Mortgage / recording date", "kind": "date"},
    "first_default_date": {"label": "First default date", "kind": "date"},
    "foreclosure_date": {"label": "Foreclosure filing date", "kind": "date", "deadline": True},
    "notice_date": {"label": "Notice date", "kind": "date", "deadline": True},
}

MONEY_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "loan_amount",
        re.compile(
            rf"(?i)(?:original\s+(?:loan\s+)?(?:principal|amount)|principal\s+(?:sum|amount|balance)\s+of|"
            rf"loan\s+(?:was\s+)?(?:in\s+the\s+(?:original\s+)?amount\s+of|amount(?:\s+of)?)|"
            rf"note\s+in\s+the\s+(?:original\s+)?(?:principal\s+)?amount(?:\s+of)?)\D{{0,48}}{MONEY}"
        ),
    ),
    (
        "monthly_payment",
        re.compile(
            rf"(?i)(?:monthly\s+(?:mortgage\s+)?payment|monthly\s+installment|"
            rf"regular\s+monthly\s+(?:payment|installment))\D{{0,40}}{MONEY}"
        ),
    ),
    (
        "outstanding_balance",
        re.compile(
            rf"(?i)(?:outstanding\s+(?:principal\s+)?balance|unpaid\s+principal(?:\s+balance)?|"
            rf"principal\s+balance(?:\s+as\s+of)?|amount\s+(?:currently\s+)?"
            rf"(?:due|owed|owing)|total\s+(?:amount\s+)?(?:due|owed))\D{{0,48}}{MONEY}"
        ),
    ),
    (
        "fees",
        re.compile(
            rf"(?i)(?:accrued\s+interest|late\s+fees|attorney['’]s?\s+fees|"
            rf"costs\s+and\s+fees|fees\s+claimed)\D{{0,40}}{MONEY}"
        ),
    ),
]

RATE_RE = re.compile(
    r"(?i)(?:interest\s+rate|annual\s+(?:percentage\s+)?rate|\bAPR\b|"
    r"at\s+the\s+(?:annual\s+)?rate\s+of|fixed\s+(?:interest\s+)?rate)"
    r"\D{0,28}(\d{1,2}(?:\.\d{1,4})?)\s*%"
)

DATE_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "loan_origination_date",
        re.compile(
            rf"(?i)(?:loan\s+origination|originated\s+on|date\s+of\s+origination|"
            rf"note\s+(?:was\s+)?(?:made|dated|executed))\D{{0,28}}({DATE_TOKEN})"
        ),
    ),
    (
        "note_date",
        re.compile(rf"(?i)(?:note\s+dated|dated\s+note|execution\s+of\s+the\s+note)\D{{0,20}}({DATE_TOKEN})"),
    ),
    (
        "mortgage_date",
        re.compile(
            rf"(?i)(?:mortgage\s+(?:was\s+)?(?:dated|recorded|executed)|"
            rf"recorded\s+on)\D{{0,24}}({DATE_TOKEN})"
        ),
    ),
    (
        "first_default_date",
        re.compile(
            rf"(?i)(?:first\s+(?:default|missed\s+payment)|date\s+of\s+(?:first\s+)?default|"
            rf"default(?:ed)?\s+on|in\s+default\s+(?:since|as\s+of))\D{{0,20}}({DATE_TOKEN})"
        ),
    ),
    (
        "foreclosure_date",
        re.compile(
            rf"(?i)(?:foreclosure\s+(?:action\s+)?(?:was\s+)?(?:filed|commenced)|"
            rf"filed\s+(?:a\s+)?(?:foreclosure|summons))\D{{0,28}}({DATE_TOKEN})"
        ),
    ),
    (
        "notice_date",
        re.compile(
            rf"(?i)(?:notice\s+of\s+(?:default|intent|sale)|90[\s-]?day\s+notice|"
            rf"RPAPL\s+1304)\D{{0,28}}({DATE_TOKEN})"
        ),
    ),
]

ATTACHED_EXHIBIT_RE = re.compile(
    r"(?i)(?:(?:annexed|attached|appended|submitted\s+herewith|hereto).{0,48}exhibit\s+([A-Z0-9]{1,3})"
    r"|exhibit\s+([A-Z0-9]{1,3}).{0,48}(?:annexed|attached|appended|hereto|herewith))"
)

NAME_ALIASES = {
    "wamu": "washington mutual",
    "wash mutual": "washington mutual",
    "wash. mutual": "washington mutual",
    "washington mutual bank": "washington mutual",
    "jpmorgan chase": "jpmorgan chase",
    "jp morgan chase": "jpmorgan chase",
    "jpmorgan chase bank": "jpmorgan chase",
    "chase bank": "jpmorgan chase",
    "wells fargo bank": "wells fargo",
    "wells fargo bank na": "wells fargo",
    "wells fargo bank n.a": "wells fargo",
    "wells fargo home mortgage": "wells fargo",
    "bank of america na": "bank of america",
    "bank of america n.a": "bank of america",
    "bank of america, n.a": "bank of america",
}


def extract_facts(text: str, limit: int = 24) -> list[dict[str, Any]]:
    body = text or ""
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    for key, pattern in MONEY_PATTERNS:
        for match in pattern.finditer(body):
            raw = match.group(1)
            value = _money(raw)
            if value is None:
                continue
            rec = _record(key, value, f"{value:,.2f}", match.group(0))
            token = (key, rec["display"])
            if token in seen:
                continue
            seen.add(token)
            facts.append(rec)
            if len(facts) >= limit:
                return facts

    for match in RATE_RE.finditer(body):
        value = float(match.group(1))
        rec = _record("interest_rate", value, f"{value}%", match.group(0))
        token = ("interest_rate", rec["display"])
        if token in seen:
            continue
        seen.add(token)
        facts.append(rec)
        if len(facts) >= limit:
            return facts

    for key, pattern in DATE_PATTERNS:
        for match in pattern.finditer(body):
            parsed = _date(match.group(1))
            if not parsed:
                continue
            rec = _record(key, parsed["value"], parsed["display"], match.group(0), extra=parsed)
            token = (key, rec["display"])
            if token in seen:
                continue
            seen.add(token)
            facts.append(rec)
            if len(facts) >= limit:
                return facts

    return facts


def referenced_exhibits(text: str) -> list[str]:
    out: list[str] = []
    for match in ATTACHED_EXHIBIT_RE.finditer(text or ""):
        tag = (match.group(1) or match.group(2) or "").upper()
        if tag and tag not in out:
            out.append(tag)
    return out


def normalize_party(name: str) -> str:
    n = re.sub(r"[.,]", "", name or "")
    n = re.sub(r"\s+", " ", n).strip().lower()
    n = re.sub(r"\b(n\/a|na|inc|llc|llp|pllc|corp|corporation|national association|n a)\b", "", n)
    n = re.sub(r"\s+", " ", n).strip()
    return NAME_ALIASES.get(n, n)


def _record(key: str, value: Any, display: str, quote: str, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    meta = FACT_META[key]
    rec = {
        "key": key,
        "label": meta["label"],
        "kind": meta["kind"],
        "value": value,
        "display": display,
        "quote": re.sub(r"\s+", " ", quote).strip()[:220],
    }
    if extra:
        rec.update(extra)
    return rec


def _money(raw: str) -> float | None:
    cleaned = re.sub(r"[^\d.]", "", raw or "")
    if not cleaned:
        return None
    try:
        return float(cleaned)
    except ValueError:
        return None


def _date(raw: str) -> dict[str, Any] | None:
    try:
        parsed = dateparser.parse(raw, fuzzy=True, default=None)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed is None:
        return None
    month_only = not re.search(r"\d{1,2}", raw) or (
        re.search(
            r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\.?\s+\d{4}",
            raw,
            re.I,
        )
        and not re.search(r"\d{1,2},", raw)
    )
    iso = parsed.date().isoformat()
    return {
        "value": iso[:7] if month_only else iso,
        "display": raw.strip(),
        "year": parsed.year,
        "month": parsed.month,
        "day": None if month_only else parsed.day,
        "month_key": f"{parsed.year:04d}-{parsed.month:02d}",
    }
