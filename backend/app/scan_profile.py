"""Route documents to forensic vs court-filing scan profiles.

Two lanes:
- Scanned PDFs / images (bills, statements, photo exhibits) → forensic forgery
  review plus OCR facts for the cross-doc case table.
- Native pleadings (motions, affidavits, transcripts) → gotcha / fact lanes;
  packaging noise (AcroForm, mixed page size, e-filing stamps) is skipped.

Blank kind with no usable peek text defaults to forensic so image-only fraud
exhibits do not silently skip ELA. E-filing stamp chrome alone never chooses
the court-filing profile — that is national, not NYSCEF-specific. Strong
pleading captions on blank kind may suggest court_filing; scan-heavy / image
media always override back to forensic in the pipeline.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

# Pleadings and procedural filings — extract facts, compare across the case.
COURT_FILING_KINDS = frozenset(
    {
        "complaint",
        "answer",
        "motion",
        "order",
        "affidavit",
        "affirmation",
        "letter",
        "stipulation",
        "transcript",
        "summons",
        "notice",
        "brief",
        "memorandum",
        "reply",
        "opposition",
        "filing",
        "judgment",
        "rji",
        "proof_of_service",
        "notice_of_pendency",
        "calendar",
    }
)

PLEADING_CAPTION_RE = re.compile(
    r"(?i)("
    r"supreme court of the state|"
    r"united states district court|"
    r"county court of the|"
    r"plaintiffs?\s*[,:]?\s*[-–—]\s*against|"
    r"please take notice|"
    r"attorney\s+affirmation|"
    r"request for judicial intervention|"
    r"note of issue|"
    r"notice of pendency|"
    r"wherefore.{0,80}demands? judgment|"
    r"notice of appearance|"
    r"certificate of merit"
    r")"
)

STATEMENT_HINT_RE = re.compile(
    r"(?i)("
    # OCR often glues tokens ("Amountdue", "Previousbalance") — allow optional space.
    r"account\s*(?:number|no\.?)|"
    r"amount\s*due|"
    r"statement\s*date|"
    r"previous\s*balance|"
    r"invoice\s*(?:number|no\.?)|"
    r"billing\s*(?:period|date)|"
    r"service\s*address|"
    r"meter\s*(?:read|reading|number)|"
    r"kwh|therms|"
    r"check\s*number|"
    r"national\s*grid|con\s*edison|keyspan|pseg"
    r")"
)

# Evidence packets — full fraudulent-document forensics.
EXHIBIT_KINDS = frozenset(
    {
        "exhibit",
        "bill",
        "statement",
        "invoice",
        "check",
        "receipt",
        "photo",
        "image",
        "evidence",
    }
)

IMAGE_SUFFIXES = frozenset(
    {".jpg", ".jpeg", ".png", ".gif", ".tif", ".tiff", ".webp", ".bmp"}
)

SCAN_HEAVY_GUESSES = frozenset({"image-only", "mostly-scanned", "scanned"})

PROFILE_FORENSIC = "forensic"
PROFILE_COURT_FILING = "court_filing"


def scan_profile_for_kind(doc_kind: str | None) -> str:
    kind = (doc_kind or "").strip().lower()
    if kind in COURT_FILING_KINDS:
        return PROFILE_COURT_FILING
    if kind in EXHIBIT_KINDS:
        return PROFILE_FORENSIC
    # Blank/unknown without peek: forensic so scanned fraud packets still pause on ELA.
    return PROFILE_FORENSIC


def is_court_filing(doc_kind: str | None) -> bool:
    return scan_profile_for_kind(doc_kind) == PROFILE_COURT_FILING


def infer_profile_from_text(text: str) -> str | None:
    """When the user left kind blank, guess pleading vs exhibit from the first pages.

    Statement-like content → forensic. Strong pleading caption only → court_filing.
    E-filing stamp chrome alone never selects court_filing.
    """
    head = (text or "")[:8000]
    if not head.strip():
        return None
    looks_like_statement = bool(STATEMENT_HINT_RE.search(head))
    looks_like_pleading = bool(PLEADING_CAPTION_RE.search(head))
    # Scanned bills/statements win the forgery lane even when stamp chrome is present.
    if looks_like_statement:
        return PROFILE_FORENSIC
    if looks_like_pleading:
        return PROFILE_COURT_FILING
    return None


def resolve_scan_profile(doc_kind: str | None, peek_text: str = "") -> str:
    """Honor an explicit kind; otherwise infer from caption / statement hints.

    Empty peek (image-only PDF extract) with blank kind → forensic, not court_filing.
    Stamp-only peek with blank kind → forensic (national; no e-file routing).
    """
    kind = (doc_kind or "").strip().lower()
    if kind in COURT_FILING_KINDS:
        return PROFILE_COURT_FILING
    if kind in EXHIBIT_KINDS:
        return PROFILE_FORENSIC
    inferred = infer_profile_from_text(peek_text)
    if inferred:
        return inferred
    return PROFILE_FORENSIC


def apply_media_override(
    profile: str,
    *,
    suffix: str = "",
    origin: dict[str, Any] | None = None,
) -> str:
    """Images and scan-heavy PDFs always take the forensic forgery lane.

    Explicit court-filing kind / caption inference lose to scan media so real
    uploads of printed-then-scanned exhibits are not skipped.
    """
    ext = (suffix or "").lower()
    if ext and not ext.startswith("."):
        ext = f".{ext}"
    if ext in IMAGE_SUFFIXES:
        return PROFILE_FORENSIC
    if ext == ".pdf" and profile == PROFILE_COURT_FILING:
        origin = origin or {}
        guess = str(origin.get("guess") or "")
        if origin.get("scan_heavy") or guess in SCAN_HEAVY_GUESSES:
            return PROFILE_FORENSIC
    return profile


def peek_pdf_text(path: Path, max_pages: int = 2) -> str:
    """Cheap first-page text for profile inference. Failures return empty."""
    if path.suffix.lower() != ".pdf":
        return ""
    try:
        from app.ingestion import open_pdf_reader

        reader = open_pdf_reader(path)
        chunks: list[str] = []
        for page in reader.pages[:max_pages]:
            chunks.append(page.extract_text() or "")
        return "\n".join(chunks)
    except Exception:
        return ""
