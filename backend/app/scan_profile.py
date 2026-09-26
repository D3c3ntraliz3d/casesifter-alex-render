"""Route documents to forensic vs court-filing scan profiles.

Two lanes:
- Scanned PDFs / images (bills, statements, photo exhibits) → forensic forgery
  review plus OCR facts for the cross-doc case table.
- Native pleadings (motions, affidavits, transcripts) → gotcha / fact lanes;
  packaging noise (AcroForm, mixed page size, e-filing stamps) is skipped.

Blank kind defaults forensic so Gemini multimodal always runs on real uploads
(the UI leaves kind blank). E-filing stamp / caption chrome alone never chooses
court_filing. Only an explicit court-filing kind on native media takes the
packaging skip. Images and scan-heavy PDFs always stay forensic.
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


def looks_like_statement_text(text: str) -> bool:
    return bool(STATEMENT_HINT_RE.search((text or "")[:8000]))


def looks_like_pleading_caption(text: str) -> bool:
    return bool(PLEADING_CAPTION_RE.search((text or "")[:8000]))


def infer_profile_from_text(text: str) -> str | None:
    """Text-only hints for blank kind.

    Statement-like → forensic. Caption alone does *not* select court_filing —
    that requires native-text confirmation in finalize_scan_profile. Stamp chrome
    alone never selects a profile.
    """
    head = (text or "")[:8000]
    if not head.strip():
        return None
    if looks_like_statement_text(head):
        return PROFILE_FORENSIC
    return None


def resolve_scan_profile(doc_kind: str | None, peek_text: str = "") -> str:
    """Honor an explicit kind; statement peeks force forensic; blank defaults forensic.

    Caption-only peeks stay forensic here. finalize_scan_profile may promote a
    blank-kind native-text PDF with a pleading caption to court_filing.
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


def _norm_suffix(suffix: str) -> str:
    ext = (suffix or "").lower()
    if ext and not ext.startswith("."):
        return f".{ext}"
    return ext


def is_scan_media(suffix: str = "", origin: dict[str, Any] | None = None) -> bool:
    """True for images and scan-heavy / image-only PDFs."""
    ext = _norm_suffix(suffix)
    if ext in IMAGE_SUFFIXES:
        return True
    if ext != ".pdf":
        return False
    origin = origin or {}
    guess = str(origin.get("guess") or "")
    return bool(origin.get("scan_heavy") or guess in SCAN_HEAVY_GUESSES)


def is_native_text_pdf(origin: dict[str, Any] | None = None) -> bool:
    origin = origin or {}
    return str(origin.get("guess") or "") == "native-text" and not origin.get("scan_heavy")


def apply_media_override(
    profile: str,
    *,
    suffix: str = "",
    origin: dict[str, Any] | None = None,
) -> str:
    """Images and scan-heavy PDFs always take the forensic forgery lane."""
    if is_scan_media(suffix, origin):
        return PROFILE_FORENSIC
    return profile


def finalize_scan_profile(
    profile: str,
    *,
    doc_kind: str | None = None,
    peek_text: str = "",
    suffix: str = "",
    origin: dict[str, Any] | None = None,
) -> str:
    """Final lane after origin guess.

    Order:
    1. Scan media (image / scan-heavy PDF) → forensic — always (Gemini + ELA).
    2. Explicit court-filing kind on native media → court_filing (packaging skip).
    3. Explicit exhibit kind → forensic.
    4. Blank kind → forensic — caption/stamp alone must not skip Gemini. Real
       NYSCEF uploads are blank-kind by default; promoting them to court_filing
       cleared whole matters in under a second with document_analysis skipped.

    ``peek_text`` is reserved for callers / statement hints already applied in
    ``resolve_scan_profile``.
    """
    del peek_text  # statement hints handled in resolve_scan_profile
    origin = origin or {}
    if is_scan_media(suffix, origin):
        return PROFILE_FORENSIC

    kind = (doc_kind or "").strip().lower()
    if kind in COURT_FILING_KINDS:
        return PROFILE_COURT_FILING
    if kind in EXHIBIT_KINDS:
        return PROFILE_FORENSIC

    return apply_media_override(profile, suffix=suffix, origin=origin)


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
