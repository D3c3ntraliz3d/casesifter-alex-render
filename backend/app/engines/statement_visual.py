"""Per-page visual read for two statement-body facts the broad pass often skips.

The model must quote the statement date, an undated payment-received line, and
any localized type-size or color change inside the activity rows. Findings use
only those returned strings. A year the model was told not to add is ignored.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.engines import finding
from app.engines.statement_dates import DATE_TOKEN, _parse_date, _parse_month_day
from app.models import Finding

_YEAR = re.compile(r"(?i)(?:['’]\s*\d{2}|\b(?:19|20)\d{2}\b)")
_MONTH_DAY = re.compile(
    r"(?i)\b((?:Jan(?:uary)?|Feb(?:ruary)?|Mar(?:ch)?|Apr(?:il)?|May|Jun(?:e)?|"
    r"Jul(?:y)?|Aug(?:ust)?|Sep(?:t(?:ember)?)?|Oct(?:ober)?|Nov(?:ember)?|"
    r"Dec(?:ember)?)\.?\s+\d{1,2})(?!\d)"
)
_STATEMENTISH = re.compile(
    r"(?i)(statement|bill|account|checking|amount|payment|charge|"
    r"verizon|citi|national\s*grid)"
)
_SIZE_COLOR = re.compile(
    r"(?i)(fonts?|typeface|type\s*sizes?|\bcolou?rs?\b|\bsizes?\b)"
)
_ROW = re.compile(
    r"(?i)(transaction|activity|bank\s+detail|description|line\s+item|\brow\b)"
)
_WHOLE_PAGE = re.compile(r"(?i)whole[- ]page|entire\s+page|scan\s+texture|flatbed")

_DATE_SCHEMA = {
    "type": "object",
    "properties": {
        "statement_date_as_printed": {"type": "string"},
        "payment_received_as_printed": {"type": "string"},
        "payment_prints_year": {"type": "boolean"},
    },
    "required": [
        "statement_date_as_printed",
        "payment_received_as_printed",
        "payment_prints_year",
    ],
}

_ROW_SCHEMA = {
    "type": "object",
    "properties": {
        "descriptions": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "date_as_printed": {"type": "string"},
                    "description_as_printed": {"type": "string"},
                    "letter_size_vs_date": {"type": "string"},
                    "ink_vs_date": {"type": "string"},
                },
                "required": [
                    "date_as_printed",
                    "description_as_printed",
                    "letter_size_vs_date",
                    "ink_vs_date",
                ],
            },
        }
    },
    "required": ["descriptions"],
}

_DATE_PROMPT = """Read this one consumer-statement page image.

Transcribe only what is printed. Do not invent a year, a city, or an amount.
Ignore court e-filing stamps (FILED, NYSCEF, INDEX NO., RECEIVED).

Return JSON:
- statement_date_as_printed: the statement date or bill date exactly as printed, or "".
- payment_received_as_printed: the payment-received (or last-payment) date exactly as
  printed, or "". If that line has a month and day but no year, do not add a year.
- payment_prints_year: true only when a year is printed on that payment line.

Do not report arithmetic.
"""

_ROW_PROMPT = """This image is a checking-activity table on a bank statement.
For each dated transaction row, transcribe the date and the description exactly as printed.
Compare the description's letters with the date on that same row.
letter_size_vs_date must be smaller, larger, or same.
ink_vs_date must be lighter, darker, different, or same.
Do not invent a row. Do not decide whether a difference is suspicious. Just compare.
"""


def statement_page_checklist(
    image_parts: list[dict[str, Any]],
    pages_sent: list[int],
    page_texts: list[str] | None,
    extras: dict[str, Any] | None = None,
) -> list[Finding]:
    """One visual read per statement page. Failures are recorded and do not raise."""
    if not image_parts:
        return []
    texts = list(page_texts or [])
    out: list[Finding] = []
    asked = 0
    for offset, part in enumerate(image_parts):
        page_no = pages_sent[offset] if offset < len(pages_sent) else offset + 1
        page_text = texts[page_no - 1] if 0 < page_no <= len(texts) else ""
        if not _worth_reading(page_text):
            continue
        asked += 1
        try:
            parsed = _read_page(part, page_no, extras)
        except Exception as exc:
            if extras is not None:
                extras["statement_checklist_error"] = str(exc)[:300]
            continue
        if not isinstance(parsed, dict):
            continue
        if extras is not None:
            reads = extras.setdefault("statement_checklist_reads", [])
            reads.append(
                {
                    "page": page_no,
                    "statement_date_as_printed": _clean(parsed.get("statement_date_as_printed"))[:120],
                    "payment_received_as_printed": _clean(parsed.get("payment_received_as_printed"))[:120],
                    "payment_prints_year": bool(parsed.get("payment_prints_year")),
                    "row_type_or_color_mismatch": bool(parsed.get("row_type_or_color_mismatch")),
                    "row_quote": _clean(parsed.get("row_quote"))[:160],
                    "row_note": _clean(parsed.get("row_note"))[:160],
                    "descriptions": [
                        {
                            "date": _clean(row.get("date_as_printed"))[:40],
                            "description": _clean(row.get("description_as_printed"))[:160],
                            "size": _clean(row.get("letter_size_vs_date"))[:20],
                            "ink": _clean(row.get("ink_vs_date"))[:20],
                        }
                        for row in (parsed.get("descriptions") or [])
                        if isinstance(row, dict)
                    ][:12],
                }
            )
        out.extend(findings_from_page_read(parsed, page_no))
    if extras is not None:
        extras["statement_checklist_pages"] = asked
    return out


def findings_from_page_read(parsed: dict[str, Any], page_no: int) -> list[Finding]:
    """Build findings only from strings the model returned for this page."""
    out: list[Finding] = []
    payment = _clean(parsed.get("payment_received_as_printed"))
    statement = _clean(parsed.get("statement_date_as_printed"))
    prints_year = bool(parsed.get("payment_prints_year")) or bool(_YEAR.search(payment))
    if payment and statement and not prints_year and _payment_is_later(statement, payment):
        quote = f"{payment} … {statement}"
        seed = f"pay|{page_no}|{payment}|{statement}"
        out.append(
            finding(
                fid="txt-stmtdate-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
                category="contextual",
                severity="critical",
                page=page_no,
                title="Payment date cannot belong to this statement date",
                explanation=(
                    f"The page image prints “{payment}” and statement date “{statement}”. "
                    "Those labels cannot coexist: the payment line shows a month and day "
                    "and no year, and that month and day fall after the statement date."
                ),
                why_suspicious=(
                    "A payment cannot already have been received on a later month than "
                    "the statement date when the payment line does not print an earlier year."
                ),
                confidence=86.0,
                evidence={
                    "analysis_type": "date_inconsistency",
                    "quoted_evidence": quote,
                    "left_raw": payment,
                    "right_raw": statement,
                    "ai_generated": True,
                    "visual_quote": True,
                },
                recommended_action=(
                    "Compare the payment-received line with the statement date on that page. "
                    "Do not supply a year the payment line does not print."
                ),
            )
        )

    if parsed.get("descriptions"):
        row = row_chrome_from_comparisons(parsed, page_no)
        if row is not None:
            out.append(row)
    elif parsed.get("row_type_or_color_mismatch"):
        row = row_chrome_finding(parsed, page_no)
        if row is not None:
            out.append(row)
    return out


def row_chrome_from_comparisons(parsed: dict[str, Any], page_no: int) -> Finding | None:
    """Flag a description the model said differs in size or ink from its own date.

    Every dated row looking different is whole-page texture, not a row signal.
    The quote is the model's description string only.
    """
    rows = [row for row in (parsed.get("descriptions") or []) if isinstance(row, dict)]
    compared: list[tuple[str, str, str]] = []
    odd: list[tuple[str, str, str]] = []
    for row in rows:
        desc = _clean(row.get("description_as_printed"))
        size = _clean(row.get("letter_size_vs_date")).lower()
        ink = _clean(row.get("ink_vs_date")).lower()
        if not desc:
            continue
        compared.append((desc, size, ink))
        if size in {"smaller", "larger"} or ink in {"lighter", "darker", "different"}:
            odd.append((desc, size, ink))
    if not odd or not compared or len(odd) >= len(compared):
        return None
    quoted = [(desc, size, ink) for desc, size, ink in odd if len(desc) >= 12]
    if not quoted:
        return None
    desc, size, ink = quoted[0]
    note_bits: list[str] = []
    if size in {"smaller", "larger"}:
        note_bits.append(f"a {size} type size")
    if ink in {"lighter", "darker", "different"}:
        note_bits.append(f"a {ink} ink color")
    note = (
        "Inside the activity rows, the description is "
        + " and ".join(note_bits)
        + " than the date printed on that same row."
    )
    return row_chrome_finding(
        {"row_quote": desc, "row_note": note, "row_type_or_color_mismatch": True},
        page_no,
    )


def row_chrome_finding(parsed: dict[str, Any], page_no: int) -> Finding | None:
    """Localized activity-row type or color only, quoted from the model."""
    quote = _clean(parsed.get("row_quote"))
    note = _clean(parsed.get("row_note"))
    if len(quote) < 12 or not note:
        return None
    if not _SIZE_COLOR.search(note):
        return None
    if _WHOLE_PAGE.search(note) and not _ROW.search(note):
        return None
    seed = f"row|{page_no}|{quote[:80]}|{note[:40]}"
    return finding(
        fid="ctx-rowchrome-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10],
        category="contextual",
        severity="medium",
        page=page_no,
        title="Localized type or color change in the activity rows",
        explanation=f"Automated document review reported: {note} Cited content: \"{quote}\"",
        why_suspicious=note,
        confidence=78.0,
        evidence={
            "analysis_type": "font_mismatch",
            "quoted_evidence": quote,
            "ai_generated": True,
            "visual_quote": True,
        },
        recommended_action=(
            "Look at the activity rows on that page. A localized size or color change "
            "inside the rows is separate from ordinary scan texture."
        ),
    )


def _payment_is_later(statement_raw: str, payment_raw: str) -> bool:
    stated = _statement_date(statement_raw)
    paid = _month_day(payment_raw)
    if stated is None or paid is None:
        return False
    month, day = paid
    return month > stated.month or (month == stated.month and day > stated.day)


def _statement_date(raw: str):
    for match in DATE_TOKEN.finditer(raw or ""):
        parsed = _parse_date(match.group(1))
        if parsed is not None:
            return parsed
    return None


def _month_day(raw: str) -> tuple[int, int] | None:
    match = _MONTH_DAY.search(raw or "")
    if not match:
        return None
    return _parse_month_day(match.group(1))


def _worth_reading(page_text: str) -> bool:
    body = (page_text or "").strip()
    if len(body) < 180 and body and not _STATEMENTISH.search(body):
        return False
    return True


def _clean(value: Any) -> str:
    return re.sub(r"\s+", " ", str(value or "")).strip()


def _read_page(
    part: dict[str, Any],
    page_no: int,
    extras: dict[str, Any] | None,
) -> dict[str, Any]:
    dates: dict[str, Any] = {}
    rows: dict[str, Any] = {}
    errors: list[str] = []
    try:
        rows = _ask(
            [
                _ROW_PROMPT,
                _body_crop(part),
            ],
            _ROW_SCHEMA,
            page_no,
            "rows",
            extras,
        )
    except Exception as exc:
        errors.append(str(exc))
    try:
        dates = _ask(
            [_DATE_PROMPT + f"\n\nThis image is PDF page {page_no}.", part],
            _DATE_SCHEMA,
            page_no,
            "dates",
            extras,
        )
    except Exception as exc:
        errors.append(str(exc))
    if errors and extras is not None:
        extras["statement_checklist_error"] = errors[-1][:300]
    if not dates and not rows:
        raise RuntimeError(errors[-1] if errors else "empty page read")
    merged = dict(dates)
    merged.update(rows)
    return merged


def _ask(
    parts: list[Any],
    schema: dict[str, Any],
    page_no: int,
    kind: str,
    extras: dict[str, Any] | None,
) -> dict[str, Any]:
    from app.config import settings
    from app.engines.contextual_logic import (
        _accumulate_gemini_usage,
        _generation_config,
        _post_gemini,
    )
    from app.gemini_tasks import TASK_D_VISUAL, task_config

    visual = task_config(TASK_D_VISUAL)
    body = _post_gemini(
        {
            "contents": [
                {
                    "role": "user",
                    "parts": [
                        {"text": part} if isinstance(part, str) else part for part in parts
                    ],
                }
            ],
            "generationConfig": _generation_config(
                schema, visual.thinking_level, model=visual.primary
            ),
        },
        task=TASK_D_VISUAL,
        step="statement_page_checklist",
        chunk_id=f"page-{page_no}-{kind}",
        timeout=settings.gemini_document_timeout_s,
    )
    if extras is not None:
        _accumulate_gemini_usage(extras, body)
    return _quiet_json(body)


def _body_crop(part: dict[str, Any]) -> dict[str, Any]:
    """Closer view of the page body so a row-level size or color change is readable."""
    import base64
    import io

    from PIL import Image

    inline = part.get("inline_data") or {}
    raw = inline.get("data") or ""
    if not raw:
        return part
    try:
        image = Image.open(io.BytesIO(base64.b64decode(raw))).convert("RGB")
    except Exception:
        return part
    width, height = image.size
    crop = image.crop((int(width * 0.04), int(height * 0.40), int(width * 0.96), int(height * 0.82)))
    buf = io.BytesIO()
    crop.save(buf, format="JPEG", quality=90)
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return {"inline_data": {"mime_type": "image/jpeg", "data": encoded}}


def _quiet_json(body: dict[str, Any]) -> dict[str, Any]:
    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(part.get("text", "") for part in parts).strip()
    if not raw:
        raise RuntimeError("Gemini returned an empty response.")
    parsed = __import__("json").loads(raw)
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini JSON was not an object.")
    return parsed
