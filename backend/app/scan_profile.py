"""Route documents to forensic vs court-filing scan profiles.

Exhibits and untyped scanned packets get the full alteration / whiteout / page-fraud
pipeline. Complaints, motions, and other pleadings are content + case-consistency only:
their packaging (AcroForm, mixed page size, e-filing stamps) is ordinary court workflow,
not exhibit forgery evidence.

Blank kind with no usable peek text defaults to forensic — image-only fraud exhibits
must not silently skip ELA. Explicit court-filing kinds and NYSCEF/pleading peeks
still take the fast court-filing path.
"""

from __future__ import annotations

import re
from pathlib import Path

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
    r"account\s+(?:number|no\.?)|"
    r"amount\s+due|"
    r"statement\s+date|"
    r"previous\s+balance|"
    r"invoice\s+(?:number|no\.?)|"
    r"kwh|therms|"
    r"check\s+number"
    r")"
)

EFILING_HEADER_RE = re.compile(
    r"(?i)\b(?:NYSCEF|FILED:\s|INDEX\s+NO\.?|RECEIVED\s+NYSCEF)\b"
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

PROFILE_FORENSIC = "forensic"
PROFILE_COURT_FILING = "court_filing"


def scan_profile_for_kind(doc_kind: str | None) -> str:
    kind = (doc_kind or "").strip().lower()
    if kind in COURT_FILING_KINDS:
        return PROFILE_COURT_FILING
    if kind in EXHIBIT_KINDS:
        return PROFILE_FORENSIC
    # Blank/unknown without peek: forensic so scanned fraud packets still pause on ELA.
    # resolve_scan_profile routes NYSCEF/pleading text peeks to court_filing.
    return PROFILE_FORENSIC


def is_court_filing(doc_kind: str | None) -> bool:
    return scan_profile_for_kind(doc_kind) == PROFILE_COURT_FILING


def infer_profile_from_text(text: str) -> str | None:
    """When the user left kind blank, guess pleading vs exhibit from the first pages."""
    head = (text or "")[:8000]
    if not head.strip():
        return None
    looks_like_statement = bool(STATEMENT_HINT_RE.search(head))
    looks_like_pleading = bool(PLEADING_CAPTION_RE.search(head))
    if looks_like_statement and not looks_like_pleading:
        return PROFILE_FORENSIC
    if looks_like_pleading:
        return PROFILE_COURT_FILING
    if EFILING_HEADER_RE.search(head) and not looks_like_statement:
        return PROFILE_COURT_FILING
    return None


def resolve_scan_profile(doc_kind: str | None, peek_text: str = "") -> str:
    """Honor an explicit kind; otherwise infer from caption / e-filing furniture.

    Empty peek (image-only PDF extract) with blank kind → forensic, not court_filing.
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
