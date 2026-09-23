"""Pull labeled litigation facts from a filing so the same fact can be compared.

Claude's case-clerk spec: extract dates, amounts, and parties with enough context
to compare loan amount to loan amount — not a bag of every dollar figure on the page.
"""

from __future__ import annotations

import re
from typing import Any

from dateutil import parser as dateparser

# A real filing can typo "$35,160.37" as "$35.160.37" (period instead of
# comma as the thousands separator — confirmed in a scanned loan modification
# exhibit, and the same document also used the correct comma elsewhere for
# the identical figure). Accepting either character as the group separator
# here — and letting `_money()` below decide which trailing group is cents —
# means that typo no longer gets read as a truncated, different dollar
# amount ("$35.16") that then "conflicts" with the correctly-punctuated
# figure elsewhere in the case.
MONEY = r"\$?\s*(\d{1,3}(?:[,.]\d{3})+(?:[,.]\d{2})?|\d{1,3}(?:[,.]\d{3})*(?:[,.]\d{2}))"
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
        "abs": 0.25,
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
    "first_default_date": {"label": "First default date", "kind": "date"},
    "foreclosure_date": {"label": "Foreclosure filing date", "kind": "date", "deadline": True},
    "notice_date": {"label": "Notice date", "kind": "date", "deadline": True},
    # Documentary graph attributes (entity-scoped). Attribute extractors — not lie types.
    "employment_start": {"label": "Employment start", "kind": "date"},
    "employment_tax_year": {"label": "Employment tax year (W-2)", "kind": "year"},
    "date_of_birth": {"label": "Date of birth", "kind": "date", "deadline": True},
    "residence_address": {"label": "Residence / occupancy address", "kind": "address"},
    "annual_income": {
        "label": "Annual income (stated or tax)",
        "kind": "money",
        "abs": 8000.0,
        "pct": 0.20,
    },
    "presence_on_date": {
        "label": "Presence / location on a date",
        "kind": "presence",
    },
    "stated_principal": {
        "label": "Stated principal",
        "kind": "money",
        "abs": 1000.0,
        "pct": 0.005,
    },
    "sale_grantee": {
        "label": "Sale / deed grantee",
        "kind": "entity",
    },
}

# Note rate vs default/penalty rate, remaining principal, and the installment
# amount move when an account is paid or goes into default. That is expected
# loan-status change, not the same fact disagreeing with itself.
LOAN_STATUS_KEYS = frozenset(
    {
        "loan_amount",
        "interest_rate",
        "monthly_payment",
        "outstanding_balance",
    }
)

# Origination / recording / “mortgage dated” are ordinary chronology, not a lie.
# Documentary graph attributes (employment_start, etc.) stay in GOTCHA_KEYS so
# same-entity conflicts can interrupt under credibility.
GOTCHA_KEYS = frozenset(key for key in FACT_META if key not in LOAN_STATUS_KEYS)

# A genuinely declared fact -- even one repeated verbatim several times
# across one document's paragraphs, exhibits, or motions -- still only ever
# states a small handful of distinct dollar figures. A scanned,
# column-scrambled payment/transaction-history table can instead reprint a
# column header like "Loan\nAmount" once per row, each time immediately
# ahead of that row's own different, unrelated transaction figure -- which
# reads, to the same context-phrase regex, as dozens of separate "loan
# amount" declarations, each a *different* number. Confirmed against a real
# Referee's Report exhibit: 24 distinct dollar values under "loan_amount",
# one per garbled table row, none of them actually the loan amount --
# meanwhile a genuinely restated single balance in the same document
# produces only 1-2 distinct values no matter how many times it repeats.
# Count distinct values, not raw hits, so restatement is never mistaken for
# a table: only a wide spread of *different* numbers for the same key marks
# it noise, and that whole key is dropped for this document rather than
# guessing which value (if any) is real.
MAX_DISTINCT_KEY_VALUES = 5

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


def extract_facts(text: str, limit: int = 40) -> list[dict[str, Any]]:
    body = text or ""
    facts: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    # Grounded documentary attributes first (entity-scoped) — general lie-detector lane.
    try:
        from app.fact_graph import extract_grounded_facts

        for rec in extract_grounded_facts(body, limit=limit):
            token = (rec["key"], f"{rec.get('entity_key')}|{rec['display']}")
            if token in seen:
                continue
            seen.add(token)
            facts.append(rec)
            if len(facts) >= limit:
                return facts
    except Exception:
        pass

    for key, pattern in MONEY_PATTERNS:
        key_records: list[dict[str, Any]] = []
        key_values: set[str] = set()
        for match in pattern.finditer(body):
            raw = match.group(1)
            # A wide "context phrase, then up to 48 non-digit characters,
            # then a number" gap can drift past its intended dollar figure
            # and land on an interest rate instead -- confirmed in a real
            # affidavit: "...Balance. Interest at the rate of 4.875%..."
            # matched "principal balance" as context and read "4.875" as a
            # $4,875 fact. A number immediately followed by "%" is a rate,
            # never a dollar amount, so it can never be this fact.
            if body[match.end() : match.end() + 2].lstrip().startswith("%"):
                continue
            value = _money(raw)
            if value is None:
                continue
            rec = _record(key, value, f"{value:,.2f}", match.group(0))
            if rec["display"] in key_values:
                continue
            key_values.add(rec["display"])
            key_records.append(rec)
        if len(key_values) > MAX_DISTINCT_KEY_VALUES:
            continue  # see MAX_DISTINCT_KEY_VALUES -- a scrambled table, not facts
        for rec in key_records:
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
    # MONEY (above) now accepts either "," or "." as the thousands separator,
    # so a raw match can look like "35.160.37" (period-typo'd) as easily as
    # "35,160.37" (correct) — figure out which trailing group is cents (the
    # last one, if it's exactly 2 digits) and treat every separator before
    # that as thousands grouping, regardless of which character it is.
    cleaned = re.sub(r"[^\d.,]", "", raw or "")
    if not cleaned:
        return None
    parts = re.split(r"[.,]", cleaned)
    parts = [p for p in parts if p]
    if not parts:
        return None
    if len(parts) == 1:
        try:
            return float(parts[0])
        except ValueError:
            return None
    *whole_parts, last = parts
    if len(last) == 2:
        whole = "".join(whole_parts) or "0"
        try:
            return float(f"{whole}.{last}")
        except ValueError:
            return None
    try:
        return float("".join(parts))
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
