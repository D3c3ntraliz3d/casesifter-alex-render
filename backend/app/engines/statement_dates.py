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

_MONTH = (
    r"(?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)"
)
DATE_TOKEN = re.compile(
    r"("
    r"\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r"|"
    # "April 2007" / "December 2011" — month and year, no day.
    + _MONTH
    + r"\.?\s*\d{4}"
    r"|"
    # OCR glue: Apr07'07 / Apr.07,'07 (space optional between month and day).
    + _MONTH
    + r"\.?\s*\d{1,2}(?:st|nd|rd|th)?"
    r"(?:,|\s*)?\s*(?:['’]\d{2}|\d{2,4})"
    r")",
    re.I,
)
# Token-optional labels: NYSCEF OCR glues ("StatementDate") and mistypes ("Bill Daie").
STATEMENT_DATE = re.compile(
    r"(?i)\b("
    r"statement\s*d(?:ate|aie)|"
    r"stat(?:e)?ment\s*date|"
    r"bill\s*da\w*|"
    r"invoice\s*date|"
    r"billing\s*date|"
    r"stm?t\.?\s*date"
    r")\s*[:#]?\s*"
    + DATE_TOKEN.pattern
)
DUE_DATE = re.compile(
    r"(?i)\b("
    r"due\s*date|"
    r"payment\s*due\s*date|"
    r"pay\s*by|"
    r"due\s*by|"
    r"(?:new\s*)?charges\s*due"
    r")\s*[:#]?\s*"
    + DATE_TOKEN.pattern
)
# "Payment Received Nov 16" with no year. A printed year (Nov 16, 2006) is a prior payment.
# Allow OCR glue: PaymentReceived, pymt received.
PAYMENT_NO_YEAR = re.compile(
    r"(?i)\b((?:payment|pymt)\s*received|last\s*payment|payment\s*posted)\s*[:\-]?\s*"
    r"((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+\d{1,2})(?!\d)"
    r"(?!\s*,?\s*(?:['’]\d{2}|\d{4}))"
)
# OCR often keeps "due by" and the year while eating the month ("due by oncemas a 2011").
DUE_BY_YEAR = re.compile(r"(?i)\bdue\s*by\b.{0,40}?((?:19|20)\d{2})\b")
PERIOD_SPAN = re.compile(
    r"(?i)\b(service\s*period|billing\s*period|service\s*from|from)\s*[:#]?\s*"
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
    for page_no, block in _statement_blocks(body):
        arrears = bool(ARREARS_OK_RE.search(block))
        for item in _conflicts_in_block(block, source, page_no):
            if arrears and not _future_due_date_impossibility(item) and not _undated_later_payment(item):
                continue
            key = item.id
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
            if len(out) >= 8:
                return out
    return out


def _undated_later_payment(item: Finding) -> bool:
    """A month-day payment with no year that falls after the statement date."""
    return "payment date cannot belong" in (item.title or "").lower()


def _future_due_date_impossibility(item: Finding) -> bool:
    """A due date years after the statement date is not an older missed installment."""
    if "due date" not in (item.title or "").lower():
        return False
    evidence = item.evidence or {}
    try:
        left = date.fromisoformat(str(evidence.get("left_date") or ""))
        right = date.fromisoformat(str(evidence.get("right_date") or ""))
    except ValueError:
        return False
    return (right - left).days > DUE_AFTER_DAYS


def _statement_blocks(text: str) -> list[tuple[int, str]]:
    pages = re.split(r"\f+", text or "")
    out: list[tuple[int, str]] = []
    for page_no, page in enumerate(pages, start=1):
        page = page.strip()
        if not page:
            continue
        labels = list(STATEMENT_DATE.finditer(page))
        if len(labels) <= 1:
            out.append((page_no, page))
            continue
        cuts = [0] + [match.start() for match in labels[1:]] + [len(page)]
        for left, right in zip(cuts, cuts[1:]):
            chunk = page[left:right].strip()
            if chunk:
                out.append((page_no, chunk))
    return out


def _conflicts_in_block(block: str, source: str, page_no: int | None = None) -> list[Finding]:
    statement = _first_date(STATEMENT_DATE, block)
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
                page=page_no,
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
                    page=page_no,
                )
            )
    dues = _all_dates(DUE_DATE, block)
    if statement:
        for due_date, due_raw, due_at in dues:
            delta = (due_date - statement[0]).days
            if delta > DUE_AFTER_DAYS or delta < -DUE_BEFORE_DAYS:
                findings.append(
                    _date_finding(
                        title="Due date cannot belong to this statement date",
                        left_raw=statement[1],
                        right_raw=due_raw,
                        left=statement[0],
                        right=due_date,
                        why=(
                            "A current account statement does not carry a due date years "
                            "away from its statement date, or a due date that predates "
                            "the bill by months."
                        ),
                        source=source,
                        quote=_quote_pair(block, statement[2], statement[1], due_at, due_raw),
                        page=page_no,
                    )
                )
    for index, (left_date, left_raw, left_at) in enumerate(dues):
        for right_date, right_raw, right_at in dues[index + 1 :]:
            if abs((right_date - left_date).days) <= DUE_AFTER_DAYS:
                continue
            if left_date <= right_date:
                earlier, later = left_date, right_date
                earlier_raw, later_raw = left_raw, right_raw
            else:
                earlier, later = right_date, left_date
                earlier_raw, later_raw = right_raw, left_raw
            findings.append(
                _date_finding(
                    title="Due date cannot belong to this statement date",
                    left_raw=earlier_raw,
                    right_raw=later_raw,
                    left=earlier,
                    right=later,
                    why=(
                        "One statement prints two due dates years apart. A current "
                        "bill does not ask for payment in a different year from its own coupon."
                    ),
                    source=source,
                    quote=_quote_pair(block, left_at, left_raw, right_at, right_raw),
                    page=page_no,
                )
            )
    parsed_years = {item[0].year for item in dues}
    if statement:
        parsed_years.add(statement[0].year)
    anchor = dues[0] if dues else statement
    for match in DUE_BY_YEAR.finditer(block):
        if _near_court_stamp(block, match.start()):
            continue
        year = int(match.group(1))
        if any(abs(year - other) < 2 for other in parsed_years):
            continue
        if anchor is None or abs(year - anchor[0].year) < 2:
            continue
        due_year = date(year, 1, 1)
        year_raw = match.group(0)
        year_label = match.group(1)
        if anchor[0] <= due_year:
            earlier, later = anchor[0], due_year
            earlier_raw, later_raw = anchor[1], year_label
        else:
            earlier, later = due_year, anchor[0]
            earlier_raw, later_raw = year_label, anchor[1]
        findings.append(
            _date_finding(
                title="Due date cannot belong to this statement date",
                left_raw=earlier_raw,
                right_raw=later_raw,
                left=earlier,
                right=later,
                why=(
                    "The bill says charges are due by a year that is not the year "
                    "of the other due date printed on the same statement."
                ),
                source=source,
                quote=_quote_pair(block, match.start(), year_raw, anchor[2], anchor[1]),
                page=page_no,
            )
        )
    findings.extend(_undated_payment_conflicts(block, statement, source, page_no))
    return findings


def _undated_payment_conflicts(
    block: str,
    statement: tuple[date, str, int] | None,
    source: str,
    page_no: int | None,
) -> list[Finding]:
    """Payment month-day with no year that is later than the statement date.

    Do not invent a year for the payment. A printed earlier year is an ordinary
    prior payment and is left alone.
    """
    if statement is None:
        return []
    out: list[Finding] = []
    seen: set[str] = set()
    for match in PAYMENT_NO_YEAR.finditer(block):
        if _near_court_stamp(block, match.start()):
            continue
        raw = re.sub(r"\s+", " ", match.group(2)).strip()
        month_day = _parse_month_day(raw)
        if not month_day:
            continue
        month, day = month_day
        stated = statement[0]
        later = month > stated.month or (month == stated.month and day > stated.day)
        if not later or raw.lower() in seen:
            continue
        seen.add(raw.lower())
        out.append(
            _date_finding(
                title="Payment date cannot belong to this statement date",
                left_raw=raw,
                right_raw=statement[1],
                left=stated,
                right=stated,
                omit_iso=True,
                why=(
                    "The payment line prints a month and day and no year. That month "
                    "and day fall after the statement date, so the payment cannot "
                    "already have been received on this statement. A payment from an "
                    "earlier year would print that year."
                ),
                source=source,
                quote=_quote_pair(block, match.start(2), raw, statement[2], statement[1]),
                page=page_no,
            )
        )
    return out


def _parse_month_day(raw: str) -> tuple[int, int] | None:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    match = re.match(
        r"^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s+(\d{1,2})$",
        text,
        re.I,
    )
    if not match:
        return None
    month = MONTHS.get(re.sub(r"\.", "", match.group(1)).lower())
    day = int(match.group(2))
    if not month or not 1 <= day <= 31:
        return None
    return month, day


def _first_date(pattern: re.Pattern[str], block: str) -> tuple[date, str, int] | None:
    found = _all_dates(pattern, block)
    return found[0] if found else None


def _all_dates(pattern: re.Pattern[str], block: str) -> list[tuple[date, str, int]]:
    found: list[tuple[date, str, int]] = []
    for match in pattern.finditer(block):
        if _near_court_stamp(block, match.start()):
            continue
        raw = match.group(2)
        parsed = _parse_date(raw)
        if parsed:
            found.append((parsed, raw, match.start()))
    return found


def _quote_pair(block: str, at_a: int, raw_a: str, at_b: int, raw_b: str) -> str:
    """Keep both labeled dates in the quote when they sit far apart on the page."""
    snippet = _snippet(block, min(at_a, at_b))
    if raw_a in snippet and raw_b in snippet:
        return snippet
    return re.sub(r"\s+", " ", f"{raw_a} … {raw_b}").strip()[:240]


def _near_court_stamp(block: str, at: int) -> bool:
    """Skip a date that sits on the e-filing stamp line itself.

    A body date on the next line is not stamp furniture. An 80-character window
    was dropping statement dates that sit directly under a NYSCEF header, which
    hid the April 2007 / December 2011 due-date catch on e-filed exhibits.

    The NYSCEF / FILED / RECEIVED stamp date must never be used as a statement
    date or due date, even when OCR glues the stamp onto the same visual line
    as bill labels.
    """
    line_start = block.rfind("\n", 0, at) + 1
    line_end = block.find("\n", at)
    if line_end < 0:
        line_end = len(block)
    line = block[line_start:line_end]
    # A real stamp line is short. A flattened scan page is one long line that
    # also contains the header, and every body date would be discarded.
    if len(line) <= 180 and COURT_STAMP.search(line):
        return True
    # Same physical stamp band when newlines were lost: FILED/NYSCEF within
    # ~50 chars before this date token, still on the stamp phrase.
    window = block[max(0, at - 50) : at]
    return bool(
        re.search(
            r"(?i)(?:FILED:|RECEIVED\s+NYSCEF|NYSCEF\s+DOC|INDEX\s+NO\.?)\s*[^.\n]{0,40}$",
            window,
        )
    )


def _parse_date(raw: str) -> date | None:
    text = re.sub(r"\s+", " ", (raw or "").strip())
    if not text:
        return None
    month_year = re.match(
        r"^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s*(\d{4})$",
        text,
        re.I,
    )
    if month_year:
        month = MONTHS.get(re.sub(r"\.", "", month_year.group(1)).lower())
        if month:
            return _safe_date(int(month_year.group(2)), month, 1)
    numeric = re.match(r"^(\d{1,2})[/-](\d{1,2})[/-](\d{2,4})$", text)
    if numeric:
        month, day, year = (int(numeric.group(1)), int(numeric.group(2)), int(numeric.group(3)))
        year = _full_year(year)
        return _safe_date(year, month, day)
    # Allow OCR glue: Apr07'07 / Apr. 07,'07 (optional spaces).
    named = re.match(
        r"^(Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
        r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
        r"Dec(?:ember)?)\.?\s*(\d{1,2})(?:st|nd|rd|th)?(?:,|\s*)?\s*(?:['’])?(\d{2,4})$",
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
    start = max(0, at - 40)
    if start > 0 and block[start:start + 1].isalnum():
        space = block.rfind(" ", 0, start)
        if space != -1 and start - space <= 24:
            start = space + 1
    return re.sub(r"\s+", " ", block[start : at + 160]).strip()[:240]


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
    page: int | None = None,
    omit_iso: bool = False,
) -> Finding:
    quoted = quote or f"{left_raw} / {right_raw}"
    seed = f"stmt-date|{title}|{left_raw}|{right_raw}|{quoted[:80]}"
    return finding(
        fid="txt-stmtdate-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
        category="contextual",
        severity="critical",
        page=page,
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
            "left_date": None if omit_iso else left.isoformat(),
            "right_date": None if omit_iso else right.isoformat(),
            "left_raw": left_raw,
            "right_raw": right_raw,
            "source": source,
            "deterministic": True,
        },
        recommended_action=(
            "Do not treat this statement as proof of residence, service, account status, "
            "or payment. Compare each labeled date on the face of the exhibit."
        ),
    )
