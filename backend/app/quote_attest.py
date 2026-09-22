"""Quote attestation — gotcha quotes must appear in the text of record."""

from __future__ import annotations

import re
from typing import Any


def _normalize(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").lower()).strip()


def _compact(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", _normalize(text))


def quote_attests(quote: str, corpus: str, *, min_chars: int = 24) -> bool:
    """True when quote (or a substantial normalized span) is found in corpus."""
    q = (quote or "").strip()
    if len(q) < 12:
        return False
    hay = corpus or ""
    if not hay:
        return False
    if q in hay:
        return True
    nq, nh = _normalize(q), _normalize(hay)
    if len(nq) >= 12 and nq in nh:
        return True
    # Sliding window on compact form for OCR spacing noise.
    cq, ch = _compact(q), _compact(hay)
    if len(cq) < min_chars:
        return len(cq) >= 8 and cq in ch
    if cq in ch:
        return True
    # Allow ~85% contiguous span match for long quotes.
    window = max(min_chars, int(len(cq) * 0.85))
    if window > len(cq):
        return False
    for start in range(0, len(cq) - window + 1, max(1, window // 4)):
        if cq[start : start + window] in ch:
            return True
    return False


def gotcha_quotes_attest(item: dict[str, Any], text_by_doc: dict[str, str]) -> bool:
    """Require claim/evidence quotes to resolve in their document text when available."""
    if item.get("tainted_support"):
        # Tainted cards are driven by forensic findings, not claim/evidence quote pairs.
        return True
    claim = str(item.get("claim_quote") or "").strip()
    evidence = str(item.get("evidence_quote") or "").strip()
    claim_id = str(item.get("claim_document_id") or "")
    evidence_id = str(item.get("document_id") or "")
    claim_text = text_by_doc.get(claim_id) or ""
    evidence_text = text_by_doc.get(evidence_id) or ""
    if claim and claim_text and not quote_attests(claim, claim_text):
        return False
    if evidence and evidence_text and not quote_attests(evidence, evidence_text):
        return False
    # If we have no corpus for a side, do not fail-open on broken OCR alone —
    # keep_board still has _ocr_broken.
    return True
