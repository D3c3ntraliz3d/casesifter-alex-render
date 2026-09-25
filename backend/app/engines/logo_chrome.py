"""Dedicated Gemini pass: logo / chrome era and field crispness on consumer statements.

Survives print-then-scan better than digital forensics. Court filings never run here.
"""

from __future__ import annotations

import base64
import hashlib
import io
import re
from pathlib import Path
from typing import Any

from app.config import settings
from app.engines import finding
from app.engines.contextual_logic import (
    KNOWN_ISSUER_RE,
    _accumulate_gemini_usage,
    _encode_source_file,
    _generation_config,
    _parse_json_payload,
    _post_gemini,
)
from app.engines.template_fabrication import looks_like_consumer_statement
from app.finding_policy import looks_like_recorded_instrument
from app.ingestion import load_image, render_pdf_pages
from app.models import Finding
from app.scan_profile import PROFILE_FORENSIC

STATEMENT_DATE_RE = re.compile(
    r"(?i)\b(?:statement\s+date|billing\s+date|bill\s+date|as\s+of|date)\s*[:\-]?\s*"
    r"("
    r"(?:January|February|March|April|May|June|July|August|September|October|November|December)\s+\d{1,2},?\s+\d{4}"
    r"|\d{1,2}[/-]\d{1,2}[/-]\d{2,4}"
    r")"
)

CHROME_SCHEMA = {
    "type": "object",
    "properties": {
        "mismatch": {"type": "boolean"},
        "confidence": {"type": "integer"},
        "issuer_observed": {"type": "string"},
        "logo_description": {"type": "string"},
        "era_note": {"type": "string"},
        "crispness_note": {"type": "string"},
        "quoted_evidence": {"type": "string"},
        "why_suspicious": {"type": "string"},
        "page": {"type": "integer"},
    },
    "required": ["mismatch", "confidence", "why_suspicious"],
}

CHROME_PROMPT = """You are checking a consumer bank/utility/telecom statement exhibit that may be
AI-generated or a bought template, then printed and scanned.

Named issuer from OCR: {issuer}
Statement date from OCR (if any): {statement_date}

Inspect the attached page image (and any text). Decide whether there is a material
logo/chrome or field-crispness problem that would survive print-then-scan:

1. Logo / brand mark does not match the named issuer.
2. Logo or letterhead is the wrong era for the statement date (successor brand too early,
   modern mark on an old bill, etc.).
3. Typed amounts/names look too crisp or modern relative to the claimed statement age and
   the surrounding form chrome.
4. Modern stock-template layout back-dated.

Set mismatch=true ONLY when you can cite a specific region or quoted text.
Do NOT flag ordinary flatbed noise, skew, or whole-page JPEG softness alone.
If unsure, mismatch=false.

Return JSON matching the schema."""


def extract_issuer(text: str) -> str | None:
    match = KNOWN_ISSUER_RE.search(text or "")
    if not match:
        return None
    return match.group(0).strip()


def extract_statement_date(text: str) -> str | None:
    match = STATEMENT_DATE_RE.search(text or "")
    if not match:
        return None
    return match.group(1).strip()


def run_logo_chrome_check(
    text: str,
    source_file: Path | None,
    *,
    scan_profile: str,
    source: str = "",
    doc_kind: str | None = None,
    extras: dict[str, Any] | None = None,
) -> list[Finding]:
    """Gemini vision check for logo/era/crispness on statement-like exhibits."""
    if scan_profile != PROFILE_FORENSIC:
        return []
    if not settings.gemini_api_key or not settings.contextual_multimodal:
        return []
    if not settings.logo_chrome_pass:
        return []
    if looks_like_recorded_instrument(text, source, doc_kind):
        return []

    kind = (doc_kind or "").strip().lower()
    kind_hint = kind in {
        "exhibit",
        "bill",
        "statement",
        "invoice",
        "receipt",
        "evidence",
        "photo",
        "image",
    }
    source_hint = bool(
        re.search(
            r"(?i)\b(bill|statement|utility|invoice|coned|con\s*edison|national\s*grid|keyspan|pseg)\b",
            source or "",
        )
    )
    statementish = looks_like_consumer_statement(text)
    sparse_ocr = len((text or "").strip()) < 80
    # Image-only / OCR-starved utility scans still need chrome review when the kind
    # or filename says bill/statement, or when we have a raster and almost no text.
    if not statementish and not kind_hint and not source_hint and not (sparse_ocr and source_file):
        return []

    issuer = extract_issuer(text)
    if not issuer:
        if source_file is None:
            return []
        issuer = "unknown — read issuer from image"
    statement_date = extract_statement_date(text) or "unknown"

    parts = _chrome_parts(text, source_file, issuer, statement_date)
    if not parts:
        return []
    try:
        body = _post_gemini(
            {
                "contents": [{"role": "user", "parts": parts}],
                "generationConfig": _generation_config(
                    CHROME_SCHEMA, settings.gemini_thinking_level
                ),
            },
            timeout=settings.gemini_document_timeout_s,
        )
        if extras is not None:
            _accumulate_gemini_usage(extras, body)
        parsed = _parse_json_payload(body)
    except Exception as exc:
        if extras is not None:
            extras["logo_chrome_error"] = str(exc)[:300]
        return []

    if not parsed.get("mismatch"):
        if extras is not None:
            extras["logo_chrome"] = "clean"
        return []

    try:
        model_confidence = float(parsed.get("confidence") or 0)
    except (TypeError, ValueError):
        model_confidence = 0.0
    if model_confidence < settings.contextual_min_confidence:
        return []

    why = str(parsed.get("why_suspicious") or "").strip()
    if not why:
        return []
    quoted = str(parsed.get("quoted_evidence") or "").strip()
    logo_note = str(parsed.get("logo_description") or parsed.get("era_note") or "").strip()
    crisp = str(parsed.get("crispness_note") or "").strip()
    observed = str(parsed.get("issuer_observed") or "").strip()
    if issuer.startswith("unknown") and observed:
        issuer = observed
    page = parsed.get("page")
    try:
        page_no = int(page) if page is not None else 1
    except (TypeError, ValueError):
        page_no = 1

    seed = f"logo_chrome|{issuer}|{statement_date}|{quoted[:80]}|{logo_note[:40]}"
    fid = "ctx-logo-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    explanation_bits = [
        f"Issuer on the page: {issuer}. Statement date cue: {statement_date}.",
        why,
    ]
    if logo_note:
        explanation_bits.append(f"Logo/chrome: {logo_note}")
    if crisp:
        explanation_bits.append(f"Field crispness: {crisp}")

    if extras is not None:
        extras["logo_chrome"] = "mismatch"

    has_hook = len(quoted) >= 12 or len(logo_note) >= 12
    return [
        finding(
            fid=fid,
            category="contextual",
            severity="medium",
            title=f"Logo / chrome mismatch — {issuer}",
            explanation=" ".join(explanation_bits)[:900],
            why_suspicious=(
                "AI-filled or stock templates that are printed and scanned often keep a wrong-era "
                "logo or typed fields that are too clean for the claimed statement age. That chrome "
                "survives flatbed scanning better than PDF metadata."
            ),
            confidence=min(60.0, model_confidence * 0.7),
            evidence={
                "analysis_type": "logo_chrome_mismatch",
                "ai_generated": True,
                "issuer": issuer,
                "statement_date": statement_date,
                "logo_note": logo_note or None,
                "crispness_note": crisp or None,
                "quoted_evidence": quoted or None,
                "page": page_no,
                "content_hook": has_hook,
            },
            recommended_action=(
                "Compare the header mark and field typography to a known-authentic statement "
                "from the same issuer and period. Confirm whether the brand existed in that "
                "form on the statement date."
            ),
        )
    ]


def _chrome_parts(
    text: str,
    source_file: Path | None,
    issuer: str,
    statement_date: str,
) -> list[dict[str, Any]]:
    prompt = CHROME_PROMPT.format(issuer=issuer, statement_date=statement_date)
    excerpt = (text or "")[:8000]
    parts: list[dict[str, Any]] = [{"text": prompt + "\n\nOCR excerpt:\n---\n" + excerpt + "\n---"}]

    file_b64, mime, _note = _encode_source_file(source_file)
    if file_b64 and mime:
        parts.append({"inline_data": {"mime_type": mime, "data": file_b64}})
        return parts

    page_b64 = _first_page_jpeg_b64(source_file)
    if page_b64:
        parts.append({"inline_data": {"mime_type": "image/jpeg", "data": page_b64}})
        return parts
    # Text-only chrome check is weak; skip rather than invent vision findings.
    return []


def _first_page_jpeg_b64(source_file: Path | None) -> str | None:
    if source_file is None or not source_file.exists():
        return None
    try:
        suffix = source_file.suffix.lower()
        if suffix == ".pdf":
            images = render_pdf_pages(source_file, max_pages=1)
            if not images:
                return None
            img = images[0]
        elif suffix in {".jpg", ".jpeg", ".png", ".gif", ".webp"}:
            img = load_image(source_file)
        else:
            return None
        buf = io.BytesIO()
        img.convert("RGB").save(buf, format="JPEG", quality=85)
        return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception:
        return None
