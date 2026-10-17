"""Exhibit-body signals that survive a garbled scan text layer.

Issuer×market×date rules are a small curated table (not open-ended model "era"
claims). Brand plus a date is not enough without a place signal on the same page.

A bank-detail line that prints two spellings of the same place (one edit
apart, with a street suffix and a state) is the line contradicting itself.
Ordinary spelling noise is not this check.
"""

from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from datetime import date
from typing import Any

from app.engines import finding
from app.engines.statement_dates import DATE_TOKEN, _near_court_stamp, _parse_date
from app.finding_policy import looks_like_recorded_instrument
from app.models import Finding
from app.scan_profile import PROFILE_FORENSIC, STATEMENT_HINT_RE

# KeySpan Energy Delivery New York served Brooklyn until this closing.
KEYSPAN_NYC_CLOSE = date(2007, 8, 24)
# PSEG Long Island began operating the LIPA system on this date.
PSEG_LI_START = date(2014, 1, 1)

# Full NYC area-code numbers. Truncated "718-345" is not this signal.
# Allow OCR glue: (718)643-4050, 718.643.4050, 7186434050.
_PHONE_NYC = re.compile(
    r"(?i)(?:\(\s*(?:718|212|347|646|917)\s*\)|\b(?:718|212|347|646|917))"
    r"\s*[-.]?\s*\d{3}\s*[-.]?\s*\d{4}\b|"
    r"\b(?:718|212|347|646|917)\d{7}\b"
)
_PHONE_LI = re.compile(
    r"(?i)(?:\(\s*(?:516|631)\s*\)|\b(?:516|631))"
    r"\s*[-.]?\s*\d{3}\s*[-.]?\s*\d{4}\b|"
    r"\b(?:516|631)\d{7}\b"
)
_BROOKLYN = re.compile(r"(?i)\bbrooklyn\b")
_LONG_ISLAND = re.compile(
    r"(?i)\b(?:long\s*island|nassau|suffolk|hempstead|huntington|babylon)\b"
)
_BILL_LABEL = re.compile(
    r"(?i)bill\s*da\w*|statement\s*d(?:ate|aie)|stat(?:e)?ment\s*date|billing\s*date"
)
_STATEMENTISH = re.compile(
    r"(?i)(\bstatement\b|\bchecking\b|amount\s*due|\bcitibank\b|\bverizon\b|national\s*grid|"
    r"pseg\s*long\s*island|beginning\s*balance|ending\s*balance|previous\s*balance|"
    r"balance\s*forward)"
)
_MONEY = re.compile(r"(?<![\d.])(-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})|-?\$?\s*\d+\.\d{2})(?![\d.])")
# OCR glue: BeginningBalance, Previousbalance, AmountDue.
_BEGIN_BAL = re.compile(r"(?i)beginning\s*balance\s*:?\s*")
_END_BAL = re.compile(r"(?i)ending\s*balance\s*:?\s*")
_PREV_BAL = re.compile(r"(?i)previous\s*balance\s*:?\s*")
_BAL_FWD = re.compile(r"(?i)balance\s*forward\s*:?\s*")
_PAYMENT = re.compile(r"(?i)payments?\s*received[^\n$]{0,40}")
# "New Charges $12.00" — not "New Charges Due: APR. 29, 2007".
_NEW_CHARGES = re.compile(
    r"(?i)(?:total\s*)?new\s*charges?(?!\s*due\b)\s*:?\s*|current\s*charges?\s*:?\s*"
)
_AMOUNT_DUE = re.compile(r"(?i)(?:total\s*)?amount\s*due\s*:?\s*")
_AMOUNT_DUE_LINE = re.compile(
    r"(?i)\b((?:total\s*)?amount\s*due)\s*:?\s*"
    r"(-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))"
)
_SUB_ADD = re.compile(
    r"(?i)total\s*subtracted\s*/?\s*added\s*:?\s*"
    r"|total\s*subtracted\s*:?\s*|total\s*added\s*:?\s*"
)
_ADDR_SPACED = re.compile(
    r"(?i)\b(\d{1,6})\s+([A-Za-z]{6,16})\s+"
    r"(BLVD|BL|ST|AVE|RD|DR|LN|CT|PL|WAY|STREET|AVENUE)\b"
    r"\s*,?\s*([A-Za-z]{6,16})\b\s*,?\s*([A-Z]{2})\b"
)
_ADDR_GLUED = re.compile(
    r"(?i)\b(\d{1,6})\s*([A-Za-z]{6,16}?)(BLVD|BL|ST|AVE|RD|DR|LN|CT|PL|WAY)\b"
    r"\s*,?\s*([A-Za-z]{6,16})\b\s*,?\s*([A-Z]{2})\b"
)
_COMMON = frozenset(
    {
        "payment",
        "balance",
        "account",
        "checking",
        "deposit",
        "transfer",
        "statement",
        "charges",
        "current",
        "heating",
        "customer",
        "service",
        "amount",
        "received",
        "previous",
        "forward",
        "authorized",
        "mortgage",
        "address",
        "street",
        "avenue",
        "number",
        "total",
        "subtracted",
        "national",
        "returned",
        "retumed",
    }
)


@dataclass(frozen=True)
class IssuerMarketRule:
    """Curated issuer × market × effective-date rule. Quotes are mandatory."""

    issuer_id: str
    issuer_label: str
    brand_re: re.Pattern[str]
    place_res: tuple[re.Pattern[str], ...]
    # Brand cannot bill this market on a statement date strictly before this day.
    not_before: date
    why: str
    brand_tokens: tuple[str, ...]


ISSUER_MARKET_RULES: tuple[IssuerMarketRule, ...] = (
    IssuerMarketRule(
        issuer_id="national_grid_nyc",
        issuer_label="National Grid",
        brand_re=re.compile(r"(?i)national\s*grid"),
        place_res=(_BROOKLYN, _PHONE_NYC),
        not_before=KEYSPAN_NYC_CLOSE,
        why=(
            f"National Grid's purchase of KeySpan closed on {KEYSPAN_NYC_CLOSE.isoformat()}. "
            "Before that date KeySpan still served New York City gas."
        ),
        brand_tokens=("national grid", "nationalgrid"),
    ),
    IssuerMarketRule(
        issuer_id="pseg_long_island",
        issuer_label="PSEG Long Island",
        brand_re=re.compile(r"(?i)pseg\s*long\s*island|pseglongisland"),
        place_res=(_LONG_ISLAND, _PHONE_LI),
        not_before=PSEG_LI_START,
        why=(
            f"PSEG Long Island began operating the LIPA system on {PSEG_LI_START.isoformat()}. "
            "Bills branded PSEG Long Island cannot predate that operator start."
        ),
        brand_tokens=("pseg long island", "pseglongisland"),
    ),
)

# Brand tokens used to ground AI historical_impossibility (no open-ended era claims).
CURATED_ISSUER_BRAND_TOKENS: frozenset[str] = frozenset(
    token for rule in ISSUER_MARKET_RULES for token in rule.brand_tokens
)


def curated_issuer_blob_match(blob: str) -> bool:
    """True when the finding text names a curated issuer brand (compact OCR OK)."""
    compact = re.sub(r"[^a-z0-9]", "", (blob or "").lower())
    if not compact:
        return False
    for token in CURATED_ISSUER_BRAND_TOKENS:
        needle = re.sub(r"[^a-z0-9]", "", token.lower())
        if len(needle) >= 6 and needle in compact:
            return True
    return False


def with_page_breaks(full_text: str, page_offsets: list[dict[str, Any]] | None) -> str:
    """Rejoin embedded text with form feeds so each statement page is its own block."""
    body = full_text or ""
    offsets = list(page_offsets or [])
    if len(offsets) < 2:
        return body
    starts = sorted(int(entry.get("start") or 0) for entry in offsets)
    parts: list[str] = []
    for index, start in enumerate(starts):
        end = starts[index + 1] if index + 1 < len(starts) else len(body)
        parts.append(body[start:end].strip())
    return "\f".join(parts)


def find_issuer_market_anachronisms(
    text: str,
    *,
    scan_profile: str,
    source: str = "",
    doc_kind: str | None = None,
) -> list[Finding]:
    """Curated issuer × market × date impossibilities with mandatory quotes."""
    if scan_profile != PROFILE_FORENSIC:
        return []
    if looks_like_recorded_instrument(text, source, doc_kind):
        return []
    out: list[Finding] = []
    seen: set[str] = set()
    for page_no, block in _pages(text):
        for item in _markets_on_page(block, source, page_no):
            if item.id in seen:
                continue
            seen.add(item.id)
            out.append(item)
    return out


def find_line_self_contradictions(
    text: str,
    *,
    scan_profile: str,
    source: str = "",
    doc_kind: str | None = None,
) -> list[Finding]:
    """Same address line prints two spellings one edit apart."""
    if scan_profile != PROFILE_FORENSIC:
        return []
    if looks_like_recorded_instrument(text, source, doc_kind):
        return []
    out: list[Finding] = []
    seen: set[str] = set()
    for page_no, block in _pages(text):
        if not _STATEMENTISH.search(block) and not STATEMENT_HINT_RE.search(block):
            continue
        for item in _lines_on_page(block, source, page_no):
            if item.id in seen:
                continue
            seen.add(item.id)
            out.append(item)
    return out


def find_statement_math_errors(
    text: str,
    *,
    scan_profile: str,
    source: str = "",
    doc_kind: str | None = None,
) -> list[Finding]:
    """Consumer-statement arithmetic that does not reconcile on the printed totals.

    Covers beginning/ending balance with total subtracted/added, previous
    balance vs payment vs balance forward, balance-forward + new charges vs
    amount due, and disagreeing Amount Due lines. Quiet when numbers add up.
    """
    if scan_profile != PROFILE_FORENSIC:
        return []
    if looks_like_recorded_instrument(text, source, doc_kind):
        return []
    out: list[Finding] = []
    seen: set[str] = set()
    for page_no, block in _pages(text):
        if not _STATEMENTISH.search(block) and not STATEMENT_HINT_RE.search(block):
            continue
        for item in _math_on_page(block, source, page_no):
            if item.id in seen:
                continue
            seen.add(item.id)
            out.append(item)
    return out


def _pages(text: str) -> list[tuple[int, str]]:
    parts = re.split(r"\f+", text or "")
    out: list[tuple[int, str]] = []
    for page_no, part in enumerate(parts, start=1):
        chunk = part.strip()
        if chunk:
            out.append((page_no, chunk))
    return out


def _markets_on_page(block: str, source: str, page_no: int) -> list[Finding]:
    hits: list[Finding] = []
    for rule in ISSUER_MARKET_RULES:
        item = _market_rule_on_page(block, source, page_no, rule)
        if item is not None:
            hits.append(item)
    return hits


def _market_rule_on_page(
    block: str, source: str, page_no: int, rule: IssuerMarketRule
) -> Finding | None:
    brand = rule.brand_re.search(block)
    if not brand:
        return None
    place = None
    for place_re in rule.place_res:
        place = place_re.search(block)
        if place:
            break
    if place is None:
        return None
    picked = _bill_date_before(block, rule.not_before)
    if picked is None:
        return None
    parsed, raw, _at = picked
    quote = _join_quotes(block, [brand.group(0), place.group(0), raw])
    if not quote:
        return None
    place_raw = place.group(0)
    seed = (
        f"issuer-era|{rule.issuer_id}|{brand.group(0).lower()}|"
        f"{place_raw.lower()}|{parsed.isoformat()}|{quote[:80]}"
    )
    city_word = bool(re.search(r"[A-Za-z]{4,}", place_raw) and not re.search(r"\d{3}", place_raw))
    why = rule.why + " "
    if city_word:
        why += (
            f"This page names {rule.issuer_label}, {place_raw}, and a statement date "
            f"before {rule.not_before.isoformat()}."
        )
    else:
        why += (
            f"This page names {rule.issuer_label}, prints a market telephone number "
            f"({place_raw}), and a statement date before {rule.not_before.isoformat()}. "
            "The city word is not required when the number is on the page."
        )
    return finding(
        fid="txt-issuer-era-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
        category="contextual",
        severity="critical",
        page=page_no,
        title="Issuer could not have billed this market on the statement date",
        explanation=(
            f"The page prints “{brand.group(0)}”, “{place_raw}”, and “{raw}”. "
            f"{raw} is before {rule.not_before.isoformat()}, when {rule.issuer_label} "
            "did not yet bill this market."
        ),
        why_suspicious=why,
        confidence=90.0,
        evidence={
            "analysis_type": "historical_impossibility",
            "quoted_evidence": quote,
            "issuer": rule.issuer_label,
            "issuer_rule": rule.issuer_id,
            "place_quote": place_raw,
            "statement_date": parsed.isoformat(),
            "source": source,
            "deterministic": True,
        },
        recommended_action=(
            "Compare the brand, the city or telephone number, and the bill date with "
            "the date that issuer began serving that market."
        ),
    )


def _bill_date_before(block: str, cutoff: date) -> tuple[date, str, int] | None:
    candidates: list[tuple[date, str, int]] = []
    for match in DATE_TOKEN.finditer(block):
        if _near_court_stamp(block, match.start()):
            continue
        raw = match.group(1)
        parsed = _parse_date(raw)
        if parsed is None or parsed >= cutoff:
            continue
        candidates.append((parsed, raw, match.start()))
    if not candidates:
        return None
    # Prefer dates in the bill-label window. Garbled "Bill Daie" lines often
    # print two period tokens; take the earliest labeled date (the bill date),
    # not a later pay-by token that still falls before the cutoff.
    label = _BILL_LABEL.search(block)
    if label:
        after = [item for item in candidates if label.end() <= item[2] <= label.end() + 120]
        if after:
            return min(after, key=lambda item: item[0])
    return min(candidates, key=lambda item: item[2])


def _lines_on_page(block: str, source: str, page_no: int) -> list[Finding]:
    findings: list[Finding] = []
    for pattern in (_ADDR_SPACED, _ADDR_GLUED):
        for match in pattern.finditer(block):
            if pattern is _ADDR_SPACED:
                left, right, state = match.group(2), match.group(4), match.group(5)
            else:
                left, right, state = match.group(2), match.group(4), match.group(5)
            if not _one_edit(left, right):
                continue
            if left.lower() in _COMMON or right.lower() in _COMMON:
                continue
            quote = re.sub(r"\s+", " ", match.group(0)).strip()
            if left.lower() not in quote.lower() or right.lower() not in quote.lower():
                continue
            seed = f"line|{left.lower()}|{right.lower()}|{state.lower()}|{quote[:80]}"
            findings.append(
                finding(
                    fid="txt-line-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
                    category="contextual",
                    severity="critical",
                    page=page_no,
                    title="Statement line prints two spellings of the same place",
                    explanation=(
                        f"One bank-detail line prints both “{left}” and “{right}” "
                        f"({state.upper()}). The line contradicts itself."
                    ),
                    why_suspicious=(
                        "A consumer-statement address line that spells the same place two "
                        "ways is not ordinary scan texture or a one-word OCR slip. Both "
                        "spellings are on the line."
                    ),
                    confidence=88.0,
                    evidence={
                        "analysis_type": "line_self_contradiction",
                        "quoted_evidence": quote,
                        "left_token": left,
                        "right_token": right,
                        "state": state.upper(),
                        "source": source,
                        "deterministic": True,
                    },
                    recommended_action=(
                        "Read that activity line on the page. Two spellings of one place "
                        "on the line the party offered are a credibility problem."
                    ),
                )
            )
            return findings
    return findings


def _one_edit(left: str, right: str) -> bool:
    a = re.sub(r"[^a-z]", "", left.lower())
    b = re.sub(r"[^a-z]", "", right.lower())
    if not a or not b or a == b or abs(len(a) - len(b)) > 1:
        return False
    if len(a) < 6 or len(b) < 6:
        return False
    # Shared prefix keeps AVENTURA/AVNTURA (prefix "AV"); rejects unrelated pairs.
    shared = 0
    for x, y in zip(a, b):
        if x != y:
            break
        shared += 1
    if shared < 2:
        return False
    if len(a) == len(b):
        return sum(x != y for x, y in zip(a, b)) == 1
    if len(a) > len(b):
        a, b = b, a
    i = j = skips = 0
    while i < len(a) and j < len(b):
        if a[i] == b[j]:
            i += 1
            j += 1
        else:
            skips += 1
            j += 1
            if skips > 1:
                return False
    return True


def _parse_money(raw: str) -> float | None:
    text = (raw or "").strip()
    if not text:
        return None
    neg = text.startswith("-") or "$-" in text.replace(" ", "")
    digits = re.sub(r"[^\d.]", "", text)
    if not digits or digits.count(".") > 1:
        return None
    try:
        value = float(digits)
    except ValueError:
        return None
    return -value if neg else value


def _money_after(label: re.Pattern[str], block: str) -> tuple[float, str] | None:
    match = label.search(block)
    if not match:
        return None
    window = block[match.end() : match.end() + 48]
    money = _MONEY.search(window)
    if not money:
        return None
    value = _parse_money(money.group(1))
    if value is None:
        return None
    return value, money.group(1).strip()


def _sub_add_totals(block: str) -> tuple[float | None, float | None, str]:
    """Return (subtracted, added, quote snippet) from Total Subtracted/Added lines."""
    combined = re.search(
        r"(?i)total\s*subtracted\s*/?\s*added\s*:?\s*"
        r"(-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))\s+"
        r"(-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
        block,
    )
    if combined:
        sub = _parse_money(combined.group(1))
        add = _parse_money(combined.group(2))
        return sub, add, combined.group(0).strip()
    sub_m = re.search(
        r"(?i)total\s*subtracted\s*:?\s*(-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
        block,
    )
    add_m = re.search(
        r"(?i)total\s*added\s*:?\s*(-?\$?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2}))",
        block,
    )
    sub = _parse_money(sub_m.group(1)) if sub_m else None
    add = _parse_money(add_m.group(1)) if add_m else None
    bits = [m.group(0).strip() for m in (sub_m, add_m) if m]
    return sub, add, " … ".join(bits)


def _payment_amount(block: str) -> tuple[float, str] | None:
    pay_label = _PAYMENT.search(block)
    if not pay_label:
        return None
    window = block[pay_label.start() : pay_label.end() + 48]
    money = _MONEY.search(window)
    if not money:
        return None
    value = _parse_money(money.group(1))
    if value is None:
        return None
    # Payments are printed as credits; treat absolute value as the payment.
    return abs(value), money.group(1).strip()


def _math_finding(
    *,
    page_no: int,
    source: str,
    title: str,
    explanation: str,
    quote: str,
    expected: float,
    printed: float,
) -> Finding:
    seed = f"math|{title}|{expected:.2f}|{printed:.2f}|{quote[:80]}"
    return finding(
        fid="txt-math-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
        category="contextual",
        severity="critical",
        page=page_no,
        title=title,
        explanation=explanation,
        why_suspicious=(
            "Printed statement totals do not reconcile. That is exhibit arithmetic, "
            "not ordinary loan-status movement or rounding on a recorded instrument."
        ),
        confidence=90.0,
        evidence={
            "analysis_type": "math_error",
            "quoted_evidence": quote[:300],
            "expected_amount": round(expected, 2),
            "printed_amount": round(printed, 2),
            "source": source,
            "deterministic": True,
        },
        recommended_action=(
            "Recompute the printed beginning/ending or previous/forward balances on "
            "the statement the party offered."
        ),
    )


def _math_on_page(block: str, source: str, page_no: int) -> list[Finding]:
    out: list[Finding] = []
    begin = _money_after(_BEGIN_BAL, block)
    end = _money_after(_END_BAL, block)
    sub, add, sub_quote = _sub_add_totals(block)
    if begin and end and sub is not None and add is not None:
        expected = round(begin[0] - sub + add, 2)
        printed = round(end[0], 2)
        if abs(expected - printed) >= 0.02:
            quote = (
                f"Beginning Balance: {begin[1]}; {sub_quote}; Ending Balance: {end[1]}"
            )
            out.append(
                _math_finding(
                    page_no=page_no,
                    source=source,
                    title="Statement ending balance does not reconcile",
                    explanation=(
                        f"Beginning {begin[1]} minus subtracted {sub:,.2f} plus added "
                        f"{add:,.2f} is {expected:,.2f}, but the page prints ending "
                        f"balance {end[1]}."
                    ),
                    quote=quote,
                    expected=expected,
                    printed=printed,
                )
            )

    prev = _money_after(_PREV_BAL, block)
    fwd = _money_after(_BAL_FWD, block)
    pay = _payment_amount(block)
    if prev and fwd and pay:
        expected = round(prev[0] - pay[0], 2)
        printed = round(fwd[0], 2)
        if abs(expected - printed) >= 0.02:
            quote = (
                f"Previous Balance: {prev[1]}; Payment Received {pay[1]}; "
                f"Balance Forward: {fwd[1]}"
            )
            out.append(
                _math_finding(
                    page_no=page_no,
                    source=source,
                    title="Statement balance forward does not reconcile",
                    explanation=(
                        f"Previous balance {prev[1]} minus payment {pay[1]} is "
                        f"{expected:,.2f}, but the page prints balance forward {fwd[1]}."
                    ),
                    quote=quote,
                    expected=expected,
                    printed=printed,
                )
            )

    new_charges = _money_after(_NEW_CHARGES, block)
    amount_due = _money_after(_AMOUNT_DUE, block)
    # Period identity: Balance Forward + New Charges = Amount Due.
    if fwd and new_charges and amount_due:
        expected = round(fwd[0] + new_charges[0], 2)
        printed = round(amount_due[0], 2)
        if abs(expected - printed) >= 0.02:
            quote = (
                f"Balance Forward: {fwd[1]}; New Charges: {new_charges[1]}; "
                f"Amount Due: {amount_due[1]}"
            )
            out.append(
                _math_finding(
                    page_no=page_no,
                    source=source,
                    title="Statement amount due does not reconcile with balance forward",
                    explanation=(
                        f"Balance forward {fwd[1]} plus new charges {new_charges[1]} is "
                        f"{expected:,.2f}, but the page prints amount due {amount_due[1]}."
                    ),
                    quote=quote,
                    expected=expected,
                    printed=printed,
                )
            )
    # Payment identity: Previous − Payment + New Charges = Amount Due (no forward line).
    elif prev and pay and new_charges and amount_due and not fwd:
        expected = round(prev[0] - pay[0] + new_charges[0], 2)
        printed = round(amount_due[0], 2)
        if abs(expected - printed) >= 0.02:
            quote = (
                f"Previous Balance: {prev[1]}; Payment Received {pay[1]}; "
                f"New Charges: {new_charges[1]}; Amount Due: {amount_due[1]}"
            )
            out.append(
                _math_finding(
                    page_no=page_no,
                    source=source,
                    title="Statement amount due does not reconcile with payment and charges",
                    explanation=(
                        f"Previous {prev[1]} minus payment {pay[1]} plus new charges "
                        f"{new_charges[1]} is {expected:,.2f}, but the page prints "
                        f"amount due {amount_due[1]}."
                    ),
                    quote=quote,
                    expected=expected,
                    printed=printed,
                )
            )

    # Two Amount Due lines that print different figures.
    due_vals: dict[float, str] = {}
    for match in _AMOUNT_DUE_LINE.finditer(block):
        value = _parse_money(match.group(2))
        if value is None:
            continue
        due_vals[round(value, 2)] = match.group(2).strip()
    if len(due_vals) >= 2:
        amounts = sorted(due_vals.items())
        left, right = amounts[0], amounts[1]
        quote = f"Amount Due: {left[1]} … Amount Due: {right[1]}"
        out.append(
            _math_finding(
                page_no=page_no,
                source=source,
                title="Statement prints disagreeing amount-due totals",
                explanation=(
                    f"The page prints amount due {left[1]} and amount due {right[1]} "
                    "on the same statement."
                ),
                quote=quote,
                expected=left[0],
                printed=right[0],
            )
        )
    return out


def _join_quotes(block: str, parts: list[str]) -> str:
    cleaned = [re.sub(r"\s+", " ", part).strip() for part in parts if part and part.strip()]
    if len(cleaned) < 2:
        return ""
    blob = re.sub(r"\s+", " ", block)
    compact_blob = re.sub(r"[^a-z0-9]", "", blob.lower())
    for part in cleaned:
        token = re.sub(r"[^a-z0-9]", "", part.lower())
        if len(token) < 4 or token not in compact_blob:
            return ""
    return " … ".join(cleaned)[:300]
