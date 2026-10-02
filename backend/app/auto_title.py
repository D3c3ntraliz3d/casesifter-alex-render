"""Heuristics to propose a unique display name / document kind for an uploaded filing."""

from __future__ import annotations

import re
from pathlib import Path

# Filename / NYSCEF type first. Caption body almost always contains "complaint"
# or "answer" and must not override AFFIDAVIT OF SERVICE, NOTICE OF MOTION, etc.
FILENAME_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("proof_of_service", re.compile(r"(?i)\b(affidavit|affirmation)\b.{0,40}\bof\s+service\b|\baos\b")),
    ("motion", re.compile(
        r"(?i)\bnotice\s+of\s+(?:cross[\s-]*)?mot(?:ion)?\b|\bcross[\s-]mot(?:ion)?\b|\bmotion\s+to\b"
    )),
    ("rji", re.compile(r"(?i)\brji\b|request\s+for\s+judicial\s+intervention")),
    ("filing", re.compile(
        r"(?i)authorization\s+for\s+electronic|statement\s+of\s+author|"
        r"certificate\s+of\s+merit"
    )),
    ("notice_of_pendency", re.compile(r"(?i)\bnotice\s+of\s+pendency\b")),
    ("calendar", re.compile(r"(?i)\b(appearance\s+detail|court\s+appearance|calendar)\b")),
    ("notice", re.compile(r"(?i)\bnotice\s+of\s+(?:entry|appearance|rejection|cross|mortgage)\b")),
    # Filename only: allow "Order" / "Proposed Order", but not "in order to" / "money order".
    ("order", re.compile(
        r"(?i)(?<!in )(?<!money )\b(?:proposed\s+)?order\b|\bdecision\s+and\s+order\b"
    )),
    ("affidavit", re.compile(r"(?i)\baffidavit\b")),
    ("affirmation", re.compile(r"(?i)\baffirmation\b")),
    ("letter", re.compile(r"(?i)\b(cover\s+)?letter\b")),
    ("stipulation", re.compile(r"(?i)\bstipulation\b")),
    ("transcript", re.compile(r"(?i)\b(transcript|deposition)\b")),
    ("bill", re.compile(r"(?i)\b(utility\s+bill|account\s+statement|bank\s+statement)\b")),
    ("summons", re.compile(r"(?i)\bsummons\b")),
    ("complaint", re.compile(r"(?i)\b(verified\s+)?complaint\b")),
    ("answer", re.compile(r"(?i)\banswer\b")),
]

# Body "order" must be a court order, not "in order to" / "money order" in bank text.
_ORDER_BODY_RE = re.compile(
    r"(?i)("
    r"\bproposed\s+order\b|"
    r"\bdecision\s+and\s+order\b|"
    r"\border\s+to\s+show\s+cause\b|"
    r"\bordered\s+that\b|"
    r"\bit\s+is\s+(?:hereby\s+)?ordered\b"
    r")"
)

# Body peek: specific types only. Never complaint/answer — those words live in every caption.
BODY_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("order", _ORDER_BODY_RE) if pair[0] == "order" else pair
    for pair in FILENAME_KIND_PATTERNS
    if pair[0]
    in {
        "proof_of_service",
        "motion",
        "affidavit",
        "affirmation",
        "order",
        "letter",
        "stipulation",
        "transcript",
        "bill",
    }
]

# ASCII hyphen plus common NYSCEF / Word en–em dashes between doc no and type.
_DASH = r"[\s_\-\u2010-\u2015]"

# Filename like EXHIBIT_S__83 or Exhibit S_41 — letter plus the docket/file number.
EXHIBIT_FILE_RE = re.compile(
    rf"(?i)(?:^|{_DASH})exhibit{_DASH}*"
    r"([A-Za-z0-9]{1,4}(?:-[A-Za-z0-9]{1,3})?)"
    rf"(?:{_DASH}+(\d{{1,4}}))?"
    rf"(?=$|{_DASH}|\.|[(])"
)

# NYSCEF type label EXHIBIT(S) — plural marker, optional letter / docket number after.
EXHIBIT_PARENS_S_RE = re.compile(
    rf"(?i)(?:^|{_DASH})exhibit\s*\(\s*s\s*\)"
    rf"(?:{_DASH}+([A-Za-z0-9]{{1,4}}))?"
    rf"(?:{_DASH}+(\d{{1,4}}))?"
)


def propose_document_title(
    filename: str,
    text_sample: str = "",
) -> tuple[str, str | None]:
    """Return (display_name, doc_kind). Filename wins over body text."""
    stem = _stem(filename)
    exhibit = exhibit_title_from_filename(filename)
    if exhibit:
        return _truncate(exhibit), "exhibit"

    cleaned = re.sub(r"[_\-]+", " ", stem)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    # Kind may come from the file body; the visible name stays filename-based
    # so two uploads cannot collapse to the same short label.
    kind = _kind_from_filename(cleaned) or _kind_from_body(text_sample[:4000])
    if kind in {"affidavit", "affirmation"} and _looks_like_service(text_sample):
        kind = "proof_of_service"
    if kind == "motion":
        motion = _motion_title(cleaned) or _motion_title(text_sample[:4000])
        if motion:
            return _truncate(_with_file_number(motion, stem)), "motion"
    if kind:
        label = {
            "complaint": "Complaint",
            "answer": "Answer",
            "order": "Order",
            "affidavit": "Affidavit",
            "affirmation": "Affirmation",
            "letter": "Letter",
            "stipulation": "Stipulation",
            "transcript": "Transcript",
            "bill": "Statement",
            "summons": "Summons",
            "proof_of_service": "Affidavit of Service",
            "filing": "Filing",
            "notice": "Notice",
            "rji": "RJI",
            "notice_of_pendency": "Notice of Pendency",
            "calendar": "Calendar",
        }.get(kind, kind.title())
        titled = _filename_title(cleaned)
        name = titled if _keeps_file_identity(titled, stem) else f"{label} — {titled}"
        return _truncate(_with_file_number(name, stem)), kind

    # Bare "exhibit" in the stem is enough — never leave these as Unknown.
    if re.search(r"(?i)\bexhibit\b", cleaned):
        return _truncate(_with_file_number(cleaned, stem)), "exhibit"
    if re.search(r"\b(complaint|motion|affidavit)\b", cleaned, re.I):
        return _truncate(_with_file_number(cleaned, stem)), None

    return _truncate(_with_file_number(cleaned or filename, stem)), None


def exhibit_title_from_filename(filename: str) -> str | None:
    stem = _stem(filename)
    # Leading NYSCEF doc number: "104 - EXHIBIT(S).pdf" / "104 EXHIBIT(S).pdf" → Exhibit 104
    leading = re.match(
        r"^(\d{1,4})(?:\s*[\-–—]\s*|\s+)(?=exhibit)",
        stem or "",
        re.I,
    )
    lead_no = leading.group(1) if leading else None

    match = EXHIBIT_FILE_RE.search(stem)
    if match:
        tag = match.group(1).upper()
        number = match.group(2)
        if number:
            return f"Exhibit {tag}_{number}"
        return f"Exhibit {tag}"
    # NYSCEF "EXHIBIT(S)" packets — do not let body "in order to" retitle these as Order.
    # Prefer the leading docket number ("32 - EXHIBIT(S)") over a trailing description token
    # ("… - S Bank Statement"), which is not an exhibit letter.
    parens = EXHIBIT_PARENS_S_RE.search(stem)
    if not parens:
        return None
    tag = (parens.group(1) or "").upper()
    number = parens.group(2)
    if lead_no:
        return f"Exhibit {lead_no}"
    if tag and number:
        return f"Exhibit {tag}_{number}"
    if tag:
        return f"Exhibit {tag}"
    return "Exhibit"


def looks_like_exhibit_filename(name: str) -> bool:
    """True when the upload name is an exhibit packet (EXHIBIT_S__41 / EXHIBIT(S) / Exhibit A)."""
    text = name or ""
    if exhibit_title_from_filename(text):
        return True
    return bool(
        re.search(
            rf"(?i)(?:^|{_DASH}|[./])exhibit(?:\s*\(\s*s\s*\))?(?:$|{_DASH}|[./(])",
            text,
        )
    ) or bool(re.search(r"(?i)\bexhibit\b", text))


def uniquify_display_name(
    name: str,
    existing: list[str] | set[str],
    filename: str = "",
) -> str:
    """Guarantee a case-unique display name. Prefer the filename's S_## token."""
    proposed = (name or "").strip() or exhibit_title_from_filename(filename) or _stem(filename) or "Untitled document"
    exhibit = exhibit_title_from_filename(filename)
    if exhibit:
        proposed = exhibit
    taken = {_norm_name(item) for item in existing if item}
    if _norm_name(proposed) not in taken:
        return _truncate(proposed)
    numbered = _with_file_number(proposed, _stem(filename))
    if _norm_name(numbered) not in taken:
        return _truncate(numbered)
    suffix = 2
    while True:
        candidate = f"{proposed} ({suffix})"
        if _norm_name(candidate) not in taken:
            return _truncate(candidate)
        suffix += 1


def _stem(filename: str) -> str:
    stem = Path(filename or "").stem
    return re.sub(r"\s*\(\d+\)\s*$", "", stem).strip()


def _kind_from_filename(haystack: str) -> str | None:
    for kind, pattern in FILENAME_KIND_PATTERNS:
        if pattern.search(haystack or ""):
            return kind
    return None


def _kind_from_body(haystack: str) -> str | None:
    for kind, pattern in BODY_KIND_PATTERNS:
        if pattern.search(haystack or ""):
            return kind
    return None


def _looks_like_service(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b("
            r"affidavit\s+of\s+service|proof\s+of\s+service|"
            r"served\s+the\s+within|i\s+served|process\s+server"
            r")\b",
            text or "",
        )
    )


def _kind_from_text(haystack: str) -> str | None:
    """Filename tokens first; body is only for specific types, never caption 'complaint'."""
    cleaned = re.sub(r"[_\-]+", " ", haystack or "")
    kind = _kind_from_filename(cleaned) or _kind_from_body(cleaned)
    if kind in {"affidavit", "affirmation"} and _looks_like_service(haystack):
        return "proof_of_service"
    return kind


def _motion_title(text: str) -> str | None:
    match = re.search(r"\bmotion\s+to\s+([a-z][a-z\s]{2,40})", text or "", re.I)
    if not match:
        return None
    detail = re.sub(r"\s+", " ", match.group(1)).strip(" .")
    return f"Motion to {detail.title()}" if detail else "Motion"


def _filename_title(cleaned: str) -> str:
    if cleaned.islower():
        return cleaned.title()
    return cleaned


def _keeps_file_identity(title: str, stem: str) -> bool:
    number = _trailing_number(stem)
    if number and re.search(rf"(?:^|[\s_]){re.escape(number)}(?:$|[\s_])", title):
        return True
    return len(title) >= 16


def _with_file_number(title: str, stem: str) -> str:
    number = _trailing_number(stem)
    if not number:
        return title
    if re.search(rf"(?:^|[\s_]|No\.?\s*){re.escape(number)}(?:$|[\s_.])", title):
        return title
    if title.lower().startswith("exhibit "):
        return f"{title}_{number}"
    return f"{title} {number}"


def _trailing_number(stem: str) -> str | None:
    match = re.search(r"(?:^|[\s_\-])(\d{1,4})$", stem or "")
    return match.group(1) if match else None


def _norm_name(value: str) -> str:
    return re.sub(r"\s+", " ", (value or "").strip().lower())


def retitle_case_documents(case_id: str) -> list[dict[str, str]]:
    """Rebuild display names from original filenames so no two files share a name."""
    from app import cases_db

    docs = cases_db.list_documents(case_id)
    snaps = {item["document_id"]: item for item in cases_db.list_case_snapshots(case_id)}
    taken: list[str] = []
    changes: list[dict[str, str]] = []
    for doc in docs:
        filename = doc.get("original_filename") or ""
        excerpt = (snaps.get(doc["id"]) or {}).get("text_excerpt") or ""
        proposed, kind = propose_document_title(filename, excerpt[:4000])
        if looks_like_exhibit_filename(filename) or looks_like_exhibit_filename(proposed):
            kind = "exhibit"
        unique = uniquify_display_name(proposed, taken, filename)
        taken.append(unique)
        fields: dict[str, str] = {}
        if unique != (doc.get("display_name") or ""):
            fields["display_name"] = unique
        current_kind = (doc.get("doc_kind") or "").strip().lower()
        if kind and kind != current_kind:
            fields["doc_kind"] = kind
        if fields:
            cases_db.update_document(doc["id"], **fields)
            changes.append(
                {
                    "id": doc["id"],
                    "before": doc.get("display_name") or "",
                    "after": unique,
                    "kind_before": current_kind,
                    "kind_after": kind or current_kind,
                }
            )
    return changes


def _truncate(value: str, limit: int = 120) -> str:
    value = value.strip() or "Untitled document"
    return value if len(value) <= limit else value[: limit - 1].rstrip() + "…"
