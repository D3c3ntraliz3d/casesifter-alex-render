"""Page-anchored quotes for facts, cross-doc findings, and gotchas."""

from __future__ import annotations

import re
from typing import Any


def build_page_offsets(extracted_pages: list[dict[str, Any]]) -> list[dict[str, int]]:
    """Char start of each page in the joined full_text (pages joined with '\\n')."""
    offsets: list[dict[str, int]] = []
    cursor = 0
    for i, page in enumerate(extracted_pages or []):
        page_no = int(page.get("page") or (i + 1))
        offsets.append({"page": page_no, "start": cursor})
        text = page.get("text") or ""
        cursor += len(text)
        if i < len(extracted_pages) - 1:
            cursor += 1  # the join newline in full_text
    return offsets


def char_to_page(offset: int, page_offsets: list[dict[str, int]] | None) -> int | None:
    if not page_offsets:
        return None
    try:
        pos = int(offset)
    except (TypeError, ValueError):
        return None
    if pos < 0:
        return None
    page = page_offsets[0].get("page")
    for entry in page_offsets:
        start = int(entry.get("start") or 0)
        if pos >= start:
            page = entry.get("page")
        else:
            break
    try:
        return int(page) if page is not None else None
    except (TypeError, ValueError):
        return None


def locate_quote(text: str, quote: str) -> tuple[int, int] | None:
    body = text or ""
    needle = (quote or "").strip()
    if not body or not needle:
        return None
    idx = body.find(needle)
    if idx >= 0:
        return idx, idx + len(needle)
    # Quotes are often whitespace-normalized; fall back to a collapsed search.
    collapsed_body = re.sub(r"\s+", " ", body)
    collapsed_needle = re.sub(r"\s+", " ", needle)
    cidx = collapsed_body.find(collapsed_needle)
    if cidx < 0:
        return None
    # Map collapsed index back approximately by walking original whitespace.
    orig_i = 0
    coll_i = 0
    start_orig = None
    while orig_i < len(body) and coll_i <= cidx + len(collapsed_needle):
        if coll_i == cidx and start_orig is None:
            start_orig = orig_i
        if coll_i == cidx + len(collapsed_needle) and start_orig is not None:
            return start_orig, orig_i
        if body[orig_i].isspace():
            if coll_i < len(collapsed_body) and collapsed_body[coll_i] == " ":
                coll_i += 1
            while orig_i + 1 < len(body) and body[orig_i + 1].isspace():
                orig_i += 1
        else:
            coll_i += 1
        orig_i += 1
    if start_orig is not None:
        return start_orig, min(len(body), start_orig + len(needle))
    return None


def enrich_fact_anchors(
    facts: list[dict[str, Any]],
    text: str,
    page_offsets: list[dict[str, int]] | None = None,
) -> list[dict[str, Any]]:
    """Fill page / char_start / char_end on facts that have a quote."""
    body = text or ""
    for rec in facts or []:
        if not isinstance(rec, dict):
            continue
        quote = str(rec.get("quote") or "")
        start = rec.get("char_start")
        end = rec.get("char_end")
        if start is None or end is None:
            span = locate_quote(body, quote)
            if span:
                start, end = span
                rec["char_start"] = start
                rec["char_end"] = end
        if rec.get("page") is None and start is not None:
            page = char_to_page(int(start), page_offsets)
            if page is not None:
                rec["page"] = page
        elif rec.get("page") is None and page_offsets:
            # Single-page docs still get page 1 when we have offsets.
            if len(page_offsets) == 1:
                rec["page"] = int(page_offsets[0].get("page") or 1)
        # Always keep quote key present for click-through.
        if quote and "quote" not in rec:
            rec["quote"] = quote
    return facts


def make_ref(
    *,
    document_id: str | None,
    sha256: str | None = None,
    page: int | None = None,
    char_start: int | None = None,
    char_end: int | None = None,
    quote: str | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    ref: dict[str, Any] = {
        "document_id": document_id,
        "sha256": sha256,
        "page": page,
        "char_start": char_start,
        "char_end": char_end,
        "quote": (quote or None) and re.sub(r"\s+", " ", str(quote)).strip()[:220],
    }
    if display_name:
        ref["display_name"] = display_name
    return ref


def ref_from_fact(
    fact: dict[str, Any] | None,
    *,
    document_id: str | None,
    sha256: str | None = None,
    text: str | None = None,
    page_offsets: list[dict[str, int]] | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    fact = fact or {}
    quote = str(fact.get("quote") or fact.get("display") or "")
    start = fact.get("char_start")
    end = fact.get("char_end")
    page = fact.get("page")
    if (start is None or end is None) and text and quote:
        span = locate_quote(text, quote)
        if span:
            start, end = span
    if page is None and start is not None:
        page = char_to_page(int(start), page_offsets)
    return make_ref(
        document_id=document_id,
        sha256=sha256,
        page=int(page) if page is not None else None,
        char_start=int(start) if start is not None else None,
        char_end=int(end) if end is not None else None,
        quote=quote or None,
        display_name=display_name,
    )


def ref_from_quote(
    *,
    document_id: str | None,
    quote: str | None,
    sha256: str | None = None,
    text: str | None = None,
    page_offsets: list[dict[str, int]] | None = None,
    display_name: str | None = None,
) -> dict[str, Any]:
    start = end = page = None
    q = (quote or "").strip()
    if text and q:
        span = locate_quote(text, q)
        if span:
            start, end = span
            page = char_to_page(start, page_offsets)
    elif page_offsets and len(page_offsets) == 1:
        page = int(page_offsets[0].get("page") or 1)
    return make_ref(
        document_id=document_id,
        sha256=sha256,
        page=page,
        char_start=start,
        char_end=end,
        quote=q or None,
        display_name=display_name,
    )
