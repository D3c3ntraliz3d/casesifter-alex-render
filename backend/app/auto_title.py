"""Heuristics to propose a display name / document kind for an uploaded filing."""

from __future__ import annotations

import re
from pathlib import Path


KIND_PATTERNS: list[tuple[str, re.Pattern[str], str]] = [
    ("complaint", re.compile(r"\b(verified\s+)?complaint\b", re.I), "Complaint"),
    ("answer", re.compile(r"\banswer\b", re.I), "Answer"),
    ("motion", re.compile(r"\bmotion\s+to\s+([a-z][a-z\s]{2,40})", re.I), "Motion"),
    ("motion", re.compile(r"\bnotice\s+of\s+motion\b", re.I), "Notice of Motion"),
    ("order", re.compile(r"\b(order|decision\s+and\s+order)\b", re.I), "Order"),
    ("affidavit", re.compile(r"\baffidavit\b", re.I), "Affidavit"),
    ("affirmation", re.compile(r"\baffirmation\b", re.I), "Affirmation"),
    ("exhibit", re.compile(r"\bexhibit\s*[\"']?([A-Z0-9\-]+)", re.I), "Exhibit"),
    ("letter", re.compile(r"\b(cover\s+)?letter\b", re.I), "Letter"),
    ("stipulation", re.compile(r"\bstipulation\b", re.I), "Stipulation"),
    ("transcript", re.compile(r"\b(transcript|deposition)\b", re.I), "Transcript"),
    ("bill", re.compile(r"\b(utility\s+bill|account\s+statement|bank\s+statement)\b", re.I), "Statement"),
    ("summons", re.compile(r"\bsummons\b", re.I), "Summons"),
]


def propose_document_title(
    filename: str,
    text_sample: str = "",
) -> tuple[str, str | None]:
    """Return (display_name, doc_kind) from filename + early page text."""
    stem = Path(filename).stem
    cleaned = re.sub(r"[_\-]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    haystack = f"{cleaned}\n{text_sample[:4000]}"

    for kind, pattern, label in KIND_PATTERNS:
        match = pattern.search(haystack)
        if not match:
            continue
        if kind == "motion" and match.lastindex:
            detail = re.sub(r"\s+", " ", match.group(1)).strip(" .")
            title = f"Motion to {detail.title()}" if detail else "Motion"
            return _truncate(title), kind
        if kind == "exhibit" and match.lastindex:
            tag = match.group(1).upper()
            return _truncate(f"Exhibit {tag}"), kind
        # Prefer a readable label; keep distinctive stem fragments when helpful.
        if kind == "exhibit":
            return _truncate(f"Exhibit — {cleaned}"), kind
        return _truncate(label if len(cleaned) < 8 else f"{label} — {cleaned}"), kind

    # Filename already looks like a court-style name.
    if re.search(r"\b(exhibit|complaint|motion|affidavit)\b", cleaned, re.I):
        return _truncate(cleaned.title() if cleaned.islower() else cleaned), None

    return _truncate(cleaned or filename), None


def _truncate(value: str, limit: int = 120) -> str:
    value = value.strip() or "Untitled document"
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
