"""Deterministic chronology checks on consumer statements.

Exhibits only. A utility bill or account statement whose own labeled dates cannot
coexist is internally false. That does not depend on a model pass.

Recorded instruments and court filings are out of scope. Demand letters that
recite an older missed installment are not a date defect.
"""

from __future__ import annotations

import hashlib
import re
from datetime import date, timedelta

from app.engines import finding
from app.finding_policy import ARREARS_OK_RE, looks_like_recorded_instrument
from app.models import Finding
from app.scan_profile import PROFILE_FORENSIC, STATEMENT_HINT_RE

MONTHS = {
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
    "jan": 1,
    "feb": 2,
    "mar": 3,
    "apr": 4,
    "jun": 6,
    "jul": 7,
    "aug": 8,
    "sep": 9,
    "sept": 9,
    "oct": 10,
    "nov": 11,
    "dec": 12,
}

DATE_TOKEN = re.compile(
    r"("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|"
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+\d{1,2}(?:st|nd|rd|th)?"
    r"(?:,|\s+)?\s*(?:['’]\d{2}|\d{2,4})"
    r")",
    re.I,
)
STATEMENT_DATE = re.compile(
    r"(?i)\b(statement\s+date|bill\s+date|invoice\s+date|billing\s+date)\s*[:#]?\s*"
    + DATE_TOKEN.pattern
)
DUE_DATE = re.compile(
    r"(?i)\b(due\s+date|payment\s+due\s+date|pay\s+by)\s*[:#]?\s*" + DATE_TOKEN.pattern
)
PERIOD_SPAN = re.compile(
    r"(?i)\b(service\s+period|billing\s+period|service\s+from|from)\s*[:#]?\s*"
    + DATE_TOKEN.pattern
    + r"\s*(?:-|–|—|to|through)\s*"
    + DATE_TOKEN.pattern
)
COURT_STAMP = re.compile(
    r"(?i)\b(?:NYSCEF|FILED:|RECEIVED\s+NYSCEF|INDEX\s+NO\.?|DOC(?:UMENT)?\.?\s*NO\.?)\b"
)
# Same statement: a print date a few weeks after the period is ordinary.
PERIOD_SLACK_BEFORE = 45
PERIOD_SLACK_AFTER = 90
# Due date years after the bill date, or well before it, is not a current bill.
DUE_AFTER_DAYS = 400
DUE_BEFORE_DAYS = 60


def find_statement_date_conflicts(
    text: str,
    *,
    scan_profile: str,
    source: str = "",
    doc_kind: str | None = None,
) -> list[Finding]:
    """Impossible labeled dates on a bank/utility/invoice exhibit."""
    if scan_profile != PROFILE_FORENSIC:
        return []
    if looks_like_recorded_instrument(text, source, doc_kind):
        return []
    body = text or ""
    if len(body) < 40 or not STATEMENT_HINT_RE.search(body):
        return []

    out: list[Finding] = []
    seen: set[str] = set()
    for block in _statement_blocks(body):
        if ARREARS_OK_RE.search(block):
            continue
        for item in _conflicts_in_block(block, source):
            key = item.id
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= 8:
                return out
    return out


def _statement_blocks(text: str) -> list[str]:
    pages = re.split(r"\f+", text or "")
    out: list[str] = []
    for page in pages:
        page = page.strip()
        if not page:
            continue
        labels = list(STATEMENT_DATE.finditer(page))
        if len(labels) <= 1:
            out.append(page)
            continue
        cuts = [0] + [match.start() for match in labels[1:]] + [len(page)]
        for left, right in zip(cuts, cuts[1:]):
            chunk = page[left:right].strip()
            if chunk:
                out.append(chunk)
    return out


def _conflicts_in_block(block: str, source: str) -> list[Finding]:
    statement = _first_date(STATEMENT_DATE, block)
    due = _first_date(DUE_DATE, block)
    period = PERIOD_SPAN.search(block)
    period_start = period_end = None
    period_start_raw = period_end_raw = ""
    if period:
        period_start = _parse_date(period.group(2))
        period_end = _parse_date(period.group(3))
        period_start_raw = period.group(2)
        period_end_raw = period.group(3)

    findings: list[Finding] = []
    if period_start and period_end and period_start > period_end:
        findings.append(
            _date_finding(
                title="Service period dates cannot coexist",
                left_raw=period_start_raw,
                right_raw=period_end_raw,
                left=period_start,
                right=period_end,
                why=(
                    "The stated service period starts after it ends. A real issuer "
                    "statement does not print an inverted billing window."
                ),
                source=source,
                quote=_snippet(block, period.start()),
            )
        )
    if statement and period_start and period_end:
        earliest = period_start - timedelta(days=PERIOD_SLACK_BEFORE)
        latest = period_end + timedelta(days=PERIOD_SLACK_AFTER)
        if statement[0] < earliest or statement[0] > latest:
            findings.append(
                _date_finding(
                    title="Statement date cannot fall with this service period",
                    left_raw=statement[1],
                    right_raw=f"{period_start_raw}–{period_end_raw}",
                    left=statement[0],
                    right=period_end,
                    why=(
                        "The statement date and the billed service period cannot "
                        "belong to the same issuer statement. Dates that far apart "
                        "mean the record cannot be trusted as proof."
                    ),
                    source=source,
                    quote=_snippet(block, statement[2]),
                )
            )
    if statement and due:
        delta = (due[0] - statement[0]).days
        if delta > DUE_AFTER_DAYS or delta < -DUE_BEFORE_DAYS:
            findings.append(
                _date_finding(
                    title="Due date cannot belong to this statement date",
                    left_raw=statement[1],
                    right_raw=due[1],
                    left=statement[0],
                    right=due[0],
                    why=(
                        "A current account statement does not carry a due date years "
                        "away from its statement date, or a due date that predates "
                        "the bill by months."
                    ),
                    source=source,
                    quote=_snippet(block, statement[2]),
                )
            )
    return findings


def _first_date(pattern: re.Pattern[str], block: str) -> tuple[date, str, int] | None:
    for match in pattern.finditer(block):
        if _near_court_stamp(block, match.start()):
            continue
        raw = match.group(2)
        parsed = _parse_date(raw)
        if parsed:
            return parsed, raw, match.start()
    return None


def _near_court_stamp(block: str, at: int) -> bool:
    window = block[max(0, at - 80) : at + 80]
    return bool(COURT_STAMP.search(window))


def _parse_date(raw: str) -> date | None:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return None
    numeric = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", text)
    if numeric:
        month, day, year = (int(numeric.group(1)), int(numeric.group(2)), int(numeric.group(3)))
        year = _full_year(year)
        return _safe_date(year, month, day)
    named = re.match(
        r"^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+(\d{1,2})(?:st|nd|rd|th)?,?\s*(?:['’])?(\d{2,4})$",
        text,
        re.I,
    )
    if named:
        month = MONTHS.get(re.sub(r"\.", "", named.group(1)).lower())
        if not month:
            return None
        return _safe_date(_full_year(int(named.group(3))), month, int(named.group(2)))
    return None


def _full_year(year: int) -> int:
    if year >= 100:
        return year
    return 2000 + year if year <= 39 else 1900 + year


def _safe_date(year: int, month: int, day: int) -> date | None:
    try:
        parsed = date(year, month, day)
    except ValueError:
        return None
    if parsed.year < 1970 or parsed.year > date.today().year + 2:
        return None
    return parsed


def _snippet(block: str, at: int) -> str:
    return re.sub(r"\s+", " ", block[max(0, at - 40) : at + 160]).strip()[:240]


def _date_finding(
    *,
    title: str,
    left_raw: str,
    right_raw: str,
    left: date,
    right: date,
    why: str,
    source: str,
    quote: str,
) -> Finding:
    quoted = quote or f"{left_raw} / {right_raw}"
    seed = f"stmt-date|{title}|{left.isoformat()}|{right.isoformat()}|{quoted[:80]}"
    return finding(
        fid="txt-stmtdate-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
        category="contextual",
        severity="high",
        title=title,
        explanation=(
            f"This exhibit’s own labels cannot coexist: “{left_raw}” and “{right_raw}”. "
            "A third-party statement offered as proof is internally false when its dates "
            "do not make a single billing cycle."
        ),
        why_suspicious=why,
        confidence=90.0,
        evidence={
            "analysis_type": "date_inconsistency",
            "quoted_evidence": quoted,
            "left_date": left.isoformat(),
            "right_date": right.isoformat(),
            "source": source,
            "deterministic": True,
        },
        recommended_action=(
            "Do not treat this statement as proof of residence, service, account status, "
            "or payment. Compare each labeled date on the face of the exhibit."
        ),
    )
