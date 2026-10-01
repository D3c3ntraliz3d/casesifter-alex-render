"""Gemini-assisted fact extraction — additive to regex/graph baselines.

Returns documentary facts with ai_generated=True and capped confidence.
Never replaces the deterministic extractors; pipeline merges both.
Logs Gemini token use per document so cost impact is visible.
"""

from __future__ import annotations

import logging
import re
from typing import Any

from app.config import settings
from app.fact_extract import _date
from app.page_anchors import enrich_fact_anchors, locate_quote

logger = logging.getLogger(__name__)

FACT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "facts": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "subject": {"type": "string"},
                    "predicate": {
                        "type": "string",
                        "enum": [
                            "employment_start",
                            "employment_end",
                            "date_of_birth",
                            "occupancy_start",
                            "employer_of_record",
                            "principal_balance",
                            "monthly_payment",
                            "interest_rate",
                            "first_default_date",
                            "late_fees",
                        ],
                    },
                    "value": {"type": "string"},
                    "page": {"type": "integer"},
                    "quote": {"type": "string"},
                    "employer": {"type": "string"},
                },
                "required": ["subject", "predicate", "value", "quote"],
            },
        }
    },
    "required": ["facts"],
}

PROMPT = """Extract grounded documentary facts from this exhibit text.
Return only facts that appear verbatim in a quote from the text.
Prefer employment dates, occupancy dates, balances, and fees that a court
could compare across documents. Do not invent values.

Text:
{text}
"""

# Cap AI-only confidence so regex remains the high-trust baseline.
AI_CONFIDENCE_CAP = 72.0
AI_MAX_FACTS = 12
AI_MAX_CHARS = 12_000


def _empty_usage(*, skipped: str | None = None) -> dict[str, Any]:
    usage: dict[str, Any] = {
        "prompt_tokens": 0,
        "candidates_tokens": 0,
        "thoughts_tokens": 0,
        "total_tokens": 0,
        "calls": 0,
        "facts_added": 0,
        "model": settings.gemini_model,
        "step": "ai_fact_extract",
    }
    if skipped:
        usage["skipped"] = skipped
    return usage


def extract_ai_facts(
    text: str,
    *,
    page_offsets: list[dict[str, int]] | None = None,
    limit: int = AI_MAX_FACTS,
    document_label: str | None = None,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Call Gemini when configured; return (facts, usage). No-op without a key."""
    body = (text or "").strip()
    label = (document_label or "document").strip() or "document"
    if not body:
        return [], _empty_usage(skipped="empty_text")
    if not settings.gemini_api_key:
        return [], _empty_usage(skipped="no_api_key")

    try:
        from app.engines.contextual_logic import (
            _generation_config,
            _parse_json_payload,
            _post_gemini,
            _usage_from_body,
        )
    except Exception as exc:
        logger.info("ai_fact_extract[%s] import skipped: %s", label, exc)
        return [], _empty_usage(skipped="import_error")

    snippet = body[:AI_MAX_CHARS]
    usage = _empty_usage()
    try:
        response = _post_gemini(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": PROMPT.format(text=snippet)}],
                    }
                ],
                "generationConfig": _generation_config(
                    FACT_SCHEMA, settings.gemini_claim_thinking_level
                ),
            },
            timeout=max(20, int(settings.gemini_timeout_s or 20)),
        )
        usage.update(_usage_from_body(response))
        usage["calls"] = 1
        parsed = _parse_json_payload(response)
    except Exception as exc:
        logger.info("ai_fact_extract[%s] skipped: %s", label, exc)
        usage["skipped"] = "gemini_error"
        usage["error"] = str(exc)[:200]
        _log_usage(label, usage)
        return [], usage

    items = parsed.get("facts") if isinstance(parsed, dict) else None
    if not isinstance(items, list):
        _log_usage(label, usage)
        return [], usage

    out: list[dict[str, Any]] = []
    for item in items[: max(1, limit)]:
        if not isinstance(item, dict):
            continue
        rec = _to_fact_record(item, body)
        if rec:
            out.append(rec)
    out = enrich_fact_anchors(out, body, page_offsets)
    usage["facts_added"] = len(out)
    _log_usage(label, usage)
    return out, usage


def _log_usage(document_label: str, usage: dict[str, Any]) -> None:
    logger.info(
        "ai_fact_extract[%s] tokens prompt=%s candidates=%s thoughts=%s total=%s "
        "calls=%s facts_added=%s skipped=%s model=%s",
        document_label,
        usage.get("prompt_tokens", 0),
        usage.get("candidates_tokens", 0),
        usage.get("thoughts_tokens", 0),
        usage.get("total_tokens", 0),
        usage.get("calls", 0),
        usage.get("facts_added", 0),
        usage.get("skipped") or "-",
        usage.get("model") or settings.gemini_model,
    )


def _to_fact_record(item: dict[str, Any], text: str) -> dict[str, Any] | None:
    predicate = str(item.get("predicate") or "").strip()
    quote = re.sub(r"\s+", " ", str(item.get("quote") or "")).strip()[:220]
    subject = re.sub(r"\s+", " ", str(item.get("subject") or "")).strip()
    value_raw = str(item.get("value") or "").strip()
    if not predicate or not quote or not value_raw:
        return None
    # Quote must attest into the source text (safety rail).
    if not locate_quote(text, quote) and quote.lower() not in text.lower():
        return None

    parsed = _date(value_raw) if predicate.endswith("_date") or predicate in {
        "employment_start",
        "employment_end",
        "date_of_birth",
        "occupancy_start",
        "first_default_date",
    } else None

    if parsed:
        value = parsed["value"]
        display = parsed["display"]
        kind = "date"
        extra = {k: parsed[k] for k in ("year", "month", "day", "month_key") if k in parsed}
    elif predicate in {"principal_balance", "monthly_payment", "late_fees", "interest_rate"}:
        try:
            value = float(re.sub(r"[^\d.]", "", value_raw) or "nan")
        except ValueError:
            return None
        if value != value:  # NaN
            return None
        kind = "rate" if predicate == "interest_rate" else "money"
        display = value_raw
        extra = {}
    else:
        value = value_raw
        display = value_raw
        kind = "text"
        extra = {}

    page = item.get("page")
    try:
        page_i = int(page) if page is not None else None
    except (TypeError, ValueError):
        page_i = None

    employer = str(item.get("employer") or "").strip() or None
    rec: dict[str, Any] = {
        "key": predicate,
        "label": predicate.replace("_", " ").title(),
        "kind": kind,
        "value": value,
        "display": display,
        "quote": quote,
        "documentary": True,
        "graph": predicate
        in {
            "employment_start",
            "employment_end",
            "date_of_birth",
            "occupancy_start",
            "employer_of_record",
        },
        "person": subject or None,
        "employer": employer,
        "entity_key": (subject or "").strip().lower() or None,
        "ai_generated": True,
        "confidence": AI_CONFIDENCE_CAP,
        "page": page_i,
        **extra,
    }
    return rec
