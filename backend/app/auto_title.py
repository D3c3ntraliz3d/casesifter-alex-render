"""Heuristics to propose a unique display name / document kind for an uploaded filing."""

from __future__ import annotations

import re
from pathlib import Path

# Client auto-retry used to upload as retry-<sha12>.pdf; auto_title then showed
# "retry <sha…>". Match only that placeholder — not titles that contain "retry".
_SYNTHETIC_AUTO_RETRY_NAME = re.compile(
    r"(?i)^retry[-_\s]+([0-9a-f]{8,16})(?:\.pdf)?$"
)


def is_synthetic_auto_retry_name(name: str | None) -> bool:
    """True for auto-retry placeholders like ``retry-a1b2c3d4e5f6.pdf`` / ``retry a1b2…``."""
    raw = (name or "").strip()
    if not raw:
        return False
    return bool(_SYNTHETIC_AUTO_RETRY_NAME.match(Path(raw).name.strip()))


# Filename / NYSCEF type first. Caption body almost always contains "complaint"
# or "answer" and must not override AFFIDAVIT OF SERVICE, NOTICE OF MOTION, etc.
FILENAME_KIND_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    ("proof_of_service", re.compile(r"(?i)\b(affidavit|affirmation)\b.{0,40}\bof\s+service\b|\baos\b")),
    ("order_to_show_cause", re.compile(r"(?i)\border\s+to\s+show\s+cause\b|\botsc\b")),
    ("motion_summary_judgment", re.compile(
        r"(?i)\b(?:motion\s+for\s+)?summary\s+judgment\b|\bmsj\b"
    )),
    ("motion_dismiss", re.compile(
        r"(?i)\bmotion\s+to\s+dismiss\b|\bpreliminary\s+objections\b"
    )),
    ("motion_vacate", re.compile(r"(?i)\bmotion\s+to\s+(?:vacate|set\s+aside)\b")),
    ("motion_reargue", re.compile(r"(?i)\bmotion\s+to\s+(?:reargue|reconsider)\b")),
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
    ("decision", re.compile(r"(?i)\bdecision(?:\s+and\s+order)?\b|\bopinion\b")),
    ("judgment", re.compile(r"(?i)\b(?:final\s+)?judgment\b|\bjfs\b")),
    # Filename only: allow "Order" / "Proposed Order", but not "in order to" / "money order".
    ("order", re.compile(
        r"(?i)(?<!in )(?<!money )\b(?:proposed\s+)?order\b"
    )),
    ("rule_19a_statement", re.compile(
        r"(?i)\brule\s*19-?a\b|\bconcise\s+statement\s+of\s+facts\b"
    )),
    ("statement_of_facts", re.compile(
        r"(?i)\bstatement\s+of\s+(?:undisputed\s+)?(?:material\s+)?facts\b"
    )),
    ("counter_statement", re.compile(
        r"(?i)\bcounter[\s-]?statement\b|\bresponse\s+to\s+statement\s+of\s+facts\b"
    )),
    ("affidavit", re.compile(r"(?i)\baffidavit\b")),
    ("affirmation", re.compile(r"(?i)\baffirmation\b")),
    ("letter", re.compile(r"(?i)\b(cover\s+)?letter\b")),
    ("stipulation", re.compile(r"(?i)\bstipulation\b")),
    ("deposition_transcript", re.compile(r"(?i)\bdeposition\b")),
    ("hearing_transcript", re.compile(r"(?i)\bhearing\s+transcript\b")),
    ("trial_transcript", re.compile(r"(?i)\btrial\s+transcript\b")),
    ("transcript", re.compile(r"(?i)\btranscript\b")),
    ("interrogatory_answers", re.compile(r"(?i)\binterrogator(?:y|ies)\b")),
    ("rfa_responses", re.compile(r"(?i)\brequests?\s+for\s+admission\b|\brfa\b")),
    ("appellate_brief", re.compile(r"(?i)\bappellate\s+brief\b|\bbrief\s+for\s+(?:appellant|appellee)\b")),
    ("reply_brief", re.compile(r"(?i)\breply\s+(?:brief|memorandum)\b")),
    ("memorandum", re.compile(r"(?i)\bmemorandum\s+of\s+law\b|\bmemoran?dum\b")),
    ("brief", re.compile(r"(?i)\bbrief\s+in\s+support\b|\bbrief\b")),
    ("opposition", re.compile(r"(?i)\bopposition\b|\bresponse\s+to\s+motion\b")),
    ("proposed_findings", re.compile(
        r"(?i)\bproposed\s+findings?\b|\bfindings?\s+of\s+fact\b"
    )),
    ("pretrial_memorandum", re.compile(r"(?i)\bpretrial\s+(?:memorandum|statement)\b")),
    ("bill", re.compile(r"(?i)\b(utility\s+bill|account\s+statement|bank\s+statement)\b")),
    ("summons", re.compile(r"(?i)\bsummons\b")),
    ("verified_pleading", re.compile(r"(?i)\bverified\s+(?:complaint|petition)\b")),
    ("third_party_complaint", re.compile(r"(?i)\bthird[\s-]?party\s+complaint\b")),
    ("counterclaim", re.compile(r"(?i)\bcounter[\s-]?claim\b")),
    ("cross_claim", re.compile(r"(?i)\bcross[\s-]?claim\b")),
    ("affirmative_defenses", re.compile(r"(?i)\baffirmative\s+defenses?\b")),
    ("new_matter", re.compile(r"(?i)\bnew\s+matter\b")),
    ("petition", re.compile(r"(?i)\bpetition\b")),
    ("complaint", re.compile(r"(?i)\bcomplaint\b")),
    ("reply", re.compile(r"(?i)\breply\s+to\s+(?:counterclaim|new\s+matter)\b")),
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
        "order_to_show_cause",
        "motion",
        "affidavit",
        "affirmation",
        "decision",
        "judgment",
        "order",
        "letter",
        "stipulation",
        "deposition_transcript",
        "transcript",
        "memorandum",
        "opposition",
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

# NYSCEF download / stamp index: 11467/2007, 11467_2007, INDEX NO. 11467/2007.
# Year may be followed by "_" (word char) in NYSCEF stems — do not require \b.
_INDEX_FROM_NAME_RE = re.compile(
    r"(?i)(?:index\s*(?:no\.?|number)?\s*[_:=\-]?\s*)?"
    r"(\d{3,7})\s*[/_\-]\s*((?:19|20)\d{2})(?!\d)"
)


def normalize_index_number(raw: str | None) -> str:
    """Canonical compare form: digits/year with a single slash (e.g. 11467/2007)."""
    text = (raw or "").strip()
    if not text:
        return ""
    m = _INDEX_FROM_NAME_RE.search(text)
    if m:
        return f"{m.group(1)}/{m.group(2)}"
    # Bare "11467/2007" already normalized-ish.
    compact = re.sub(r"\s+", "", text)
    m2 = re.fullmatch(r"(\d{3,7})[/_\-]((?:19|20)\d{2})", compact)
    if m2:
        return f"{m2.group(1)}/{m2.group(2)}"
    return compact.lower()


def parse_index_from_filename(filename: str | None) -> str | None:
    """Pull a court index from a NYSCEF-style filename when present."""
    stem = _stem(filename or "")
    if not stem:
        return None
    m = _INDEX_FROM_NAME_RE.search(stem)
    if not m:
        return None
    return f"{m.group(1)}/{m.group(2)}"


def index_mismatch_warning(
    filename: str | None,
    matter_index: str | None,
) -> dict[str, str] | None:
    """Warn when filename index ≠ matter index (soft-block; upload still allowed)."""
    file_idx = parse_index_from_filename(filename)
    matter_norm = normalize_index_number(matter_index)
    if not file_idx or not matter_norm:
        return None
    if normalize_index_number(file_idx) == matter_norm:
        return None
    return {
        "filename_index": file_idx,
        "matter_index": matter_norm,
        "message": (
            f"This file’s NYSCEF index ({file_idx}) does not match this matter’s "
            f"index ({matter_norm}). Cross-index exhibits can pollute Case Impact — "
            "confirm before relying on the board."
        ),
    }


def propose_document_title(
    filename: str,
    text_sample: str = "",
) -> tuple[str, str | None]:
    """Return (display_name, doc_kind). Filename wins over body text."""
    stem = _stem(filename)
    exhibit = exhibit_title_from_filename(filename)
    if exhibit:
        # Exhibit packet title stays; statement body → bill so the Kind column
        # can show Statement/bill without a manual "fix kinds" pass.
        try:
            from app.scan_profile import looks_like_statement_text
        except Exception:
            looks_like_statement_text = lambda _t: False  # noqa: E731
        if text_sample and looks_like_statement_text(text_sample):
            return _truncate(exhibit), "bill"
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
        try:
            from app.doc_kinds import kind_label as _kind_label
        except Exception:
            _kind_label = lambda k: (k or "").replace("_", " ").title()  # noqa: E731
        label = {
            "complaint": "Complaint",
            "answer": "Answer",
            "order": "Order",
            "decision": "Decision",
            "judgment": "Judgment",
            "affidavit": "Affidavit",
            "affirmation": "Affirmation",
            "letter": "Letter",
            "stipulation": "Stipulation",
            "transcript": "Transcript",
            "deposition_transcript": "Deposition",
            "bill": "Statement",
            "summons": "Summons",
            "proof_of_service": "Affidavit of Service",
            "filing": "Filing",
            "notice": "Notice",
            "rji": "RJI",
            "notice_of_pendency": "Notice of Pendency",
            "calendar": "Calendar",
            "memorandum": "Memorandum",
            "brief": "Brief",
            "opposition": "Opposition",
            "petition": "Petition",
            "counterclaim": "Counterclaim",
        }.get(kind) or _kind_label(kind)
        # "AFFIRMATION AFFIDAVIT" / "AFFIRMATION AFFIDAVIT OF SERVICE" → Affidavit / Affidavit of Service
        # Keep distinguishing residue from the filename (e.g. "– Pete Harley").
        if kind == "proof_of_service" and re.search(
            r"(?i)\b(affidavit|affirmation)\b.{0,40}\bof\s+service\b", cleaned
        ):
            residue = _service_name_residue(cleaned)
            base = "Affidavit of Service" + (f" – {residue}" if residue else "")
            return _truncate(_with_file_number(base, stem)), kind
        if kind in {"affidavit", "affirmation"} and re.search(
            r"(?i)^\s*(affirmation\s+)?affidavit(\s+or\s+affirmation)?\s*$", cleaned
        ):
            return _truncate(_with_file_number("Affidavit", stem)), "affidavit"
        if kind in {"affidavit", "affirmation"} and re.search(
            r"(?i)\baffirmation\b.+\baffidavit\b|\baffidavit\b.+\baffirmation\b", cleaned
        ):
            titled = _filename_title(cleaned)
            if re.search(r"(?i)\bof\s+service\b", cleaned):
                return _truncate(_with_file_number("Affidavit of Service", stem)), "proof_of_service"
            name = "Affidavit" if len(cleaned) < 40 else (
                titled if _keeps_file_identity(titled, stem) else f"Affidavit — {titled}"
            )
            return _truncate(_with_file_number(name, stem)), "affidavit"
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
    # ("… - S Bank Statement"), which is not an exhibit letter. Keep the description when present.
    parens = EXHIBIT_PARENS_S_RE.search(stem)
    if not parens:
        return None
    tag = (parens.group(1) or "").upper()
    number = parens.group(2)
    rest = stem[parens.end() :].strip(" \t_-–—")
    if lead_no:
        return f"Exhibit {lead_no} — {rest}" if rest else f"Exhibit {lead_no}"
    if tag and number:
        base = f"Exhibit {tag}_{number}"
        return f"{base} — {rest}" if rest else base
    if tag:
        return f"Exhibit {tag} — {rest}" if rest else f"Exhibit {tag}"
    return f"Exhibit — {rest}" if rest else "Exhibit"


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


def _service_name_residue(cleaned: str) -> str:
    """Strip generic Affidavit/Affirmation of Service chrome; keep the person/entity."""
    text = cleaned or ""
    text = re.sub(
        r"(?i)\b(affirmation\s+)?affidavit(\s+or\s+affirmation)?\s+of\s+service\b",
        " ",
        text,
    )
    text = re.sub(r"(?i)\baffirmation\s+of\s+service\b", " ", text)
    text = re.sub(r"(?i)\baos\b", " ", text)
    text = re.sub(r"(?i)\b(exhibit|ex)\s*[_\-]?\s*[a-z0-9]+\b", " ", text)
    text = re.sub(r"[_\-]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip(" –—-")
    if not text or len(text) < 2:
        return ""
    # Drop trailing file numbers already handled by _with_file_number.
    text = re.sub(r"(?:^|[\s_\-])\d{1,4}$", "", text).strip()
    if not text or len(text) < 2:
        return ""
    return _filename_title(text)[:80]


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
