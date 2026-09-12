"""Contextual fraud detection.

Extracts entities (organizations, dates, locations), checks whether the story the
document tells is internally possible, and optionally calls Google Gemini in two
distinct modes.

Three tiers, each requiring more explicit opt-in than the last:

1. Local only (always on). Entity extraction plus deterministic timeline checks.
   Nothing leaves the server.
2. Claim verification (needs GEMINI_API_KEY). Sends only a generated question,
   e.g. 'Was "Acme Freight, LLC" in operation on 2019-03-01?'. No document
   content is transmitted.
3. Document analysis (needs CONTEXTUAL_MULTIMODAL=true in addition to the key).
   Uploads the original file natively (the PDF or image itself, not a re-render)
   along with the extracted text for comprehensive review: date logic, arithmetic,
   text overlays, graphic manipulation, font mismatches. This transmits the exhibit
   itself to a third party, so it is off by default and must be enabled deliberately.
   Do not enable it for sealed, privileged, or otherwise restricted evidence.

Evidentiary weight. A language model's assertion is a research lead, not proof.
Every model-sourced finding is capped at medium severity, has its confidence
discounted, and carries ai_generated=True so the report labels it for the reader.
No model output can drive a report to red on its own.
"""

from __future__ import annotations

import base64
import hashlib
import json
import re
import threading
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

from dateutil import parser as dateparser

from app.config import settings
from app.engines import finding, timed_module
from app.finding_policy import is_header_or_arrears_noise, is_recorded_instrument_math_noise
from app.models import Finding, ModuleResult

ORG_SUFFIXES = (
    r"Inc|Incorporated|LLC|L\.L\.C|LLP|L\.L\.P|LP|PLLC|PC|P\.C|Corp|Corporation|"
    r"Company|Co|Ltd|Limited|Holdings|Group|Partners|Associates|Bank|Trust|"
    r"Enterprises|Industries|Systems|Solutions|Services|Technologies|Capital|Realty"
)
# Horizontal whitespace only: an entity name never spans a line break, and allowing
# \s+ here swallows the preceding heading (e.g. "INVOICE Meridian Freight Holdings").
ORG_RE = re.compile(
    rf"\b((?:[A-Z][A-Za-z&'’\-\.]{{1,24}},?[ \t]+){{1,5}}(?:{ORG_SUFFIXES})\.?)(?=[\s,;:.)]|$)"
)

# Banks and utilities often appear without LLC/Inc suffixes, so the suffix regex misses them.
# Matching known exhibit issuers unlocks tier-2 public-record questions
# (was issuer X operating at place Y on date Z?) without asserting any market-entry fact.
KNOWN_ISSUERS = (
    "National Grid",
    "Nationalgrid",
    "Verizon",
    "Citibank",
    "Citi Bank",
    "KeySpan",
    "Con Edison",
    "Bank of America",
    "Chase",
    "JPMorgan",
    "Wells Fargo",
    "Capital One",
    "PSEG",
    "AT&T",
    "T-Mobile",
    "Spectrum",
    "Comcast",
)
# Issuers whose service territory is New York–centric; never pair them with a Florida address
# when a NY location is also present in the exhibit.
NY_MARKET_ISSUERS = {
    "National Grid",
    "Con Edison",
    "Verizon",
    "KeySpan",
    "PSEG",
}
KNOWN_ISSUER_RE = re.compile(
    r"\b(" + "|".join(re.escape(name) for name in sorted(KNOWN_ISSUERS, key=len, reverse=True)) + r")\b",
    re.I,
)

STATE_ABBR = (
    "AL|AK|AZ|AR|CA|CO|CT|DE|FL|GA|HI|ID|IL|IN|IA|KS|KY|LA|ME|MD|MA|MI|MN|MS|MO|"
    "MT|NE|NV|NH|NJ|NM|NY|NC|ND|OH|OK|OR|PA|RI|SC|SD|TN|TX|UT|VT|VA|WA|WV|WI|WY|DC"
)
LOCATION_RE = re.compile(
    rf"\b([A-Z][a-zA-Z\.\-]+(?:[ \t]+[A-Z][a-zA-Z\.\-]+){{0,3}}),[ \t]*({STATE_ABBR})\b"
    rf"(?:[ \t]+(\d{{5}}))?"
)

DATE_RES = (
    re.compile(r"\b(\d{4}-\d{2}-\d{2})\b"),
    re.compile(r"\b(\d{1,2}[/\-]\d{1,2}[/\-]\d{2,4})\b"),
    # Allow "Nov 30,2017" (no space after the comma); OCR often drops it.
    re.compile(
        r"\b((?:January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2},?\s*\d{4})\b",
        re.I,
    ),
    re.compile(
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December),?\s*\d{4})\b",
        re.I,
    ),
)

# Court e-filing headers (NYSCEF and similar) stamp every page. Their dates are about
# the filing event / case index year, not the exhibit's internal chronology.
COURT_FILING_HEADER_RE = re.compile(
    r"\b(?:NYSCEF|FILED:|RECEIVED\s+NYSCEF|INDEX\s+NO\.?|DOC(?:UMENT)?\.?\s*NO\.?)\b",
    re.I,
)
COURT_STAMP_DATE_CONTEXT_RE = re.compile(
    r"(?:FILED:\s*[A-Z][A-Z .'-]{0,40}CLERK\s+|RECEIVED\s+NYSCEF:\s*|RECEIVED:\s*)"
    r"(\d{1,2}[/-]\d{1,2}[/-]\d{2,4}(?:\s+\d{1,2}:\d{2}\s*[AP]M)?)",
    re.I,
)
INDEX_YEAR_RE = re.compile(r"INDEX\s+NO\.?\s*[\dA-Z\-]+\s*/\s*(\d{2,4})", re.I)

# Capitalized phrases that match the org pattern but are page furniture, not parties.
ORG_STOPWORDS = {
    "exhibit",
    "attachment",
    "appendix",
    "plaintiff",
    "defendant",
    "the company",
    "the group",
    "case",
    "partners",
    "partners, pllc",
    "partners pllc",
}

# PDF text extraction joins lines with spaces, so a heading or street line runs straight
# into the name below it. These markers let us cut everything left of the real entity.
LABEL_MARKERS = {
    "invoice", "exhibit", "attachment", "appendix", "statement", "receipt", "agreement",
    "contract", "memorandum", "bill", "attn", "re", "subject", "from", "to", "page",
    "remittance", "vendor", "customer", "client",
}
STREET_MARKERS = {
    "street", "st", "avenue", "ave", "road", "rd", "boulevard", "blvd", "drive", "dr",
    "lane", "ln", "way", "court", "ct", "place", "pl", "parkway", "pkwy", "highway",
    "hwy", "suite", "ste", "floor", "fl", "unit", "apt", "box", "po", "circle", "terrace",
}
ORG_MARKERS = {
    "llc", "inc", "corp", "corporation", "company", "co", "ltd", "limited", "group",
    "holdings", "partners", "associates", "bank", "trust", "llp", "lp", "pllc", "pc",
    "enterprises", "industries", "systems", "solutions", "services", "technologies",
    "capital", "realty",
}

GEMINI_ENDPOINT = "https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent"

# Formats Gemini accepts natively. Sending the original file rather than a re-render
# preserves vector text, embedded fonts, and per-object layering, all of which are the
# very artifacts an overlay or paste leaves behind. Office formats have no native
# equivalent, so those exhibits fall back to text-only analysis.
NATIVE_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".txt": "text/plain",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".png": "image/png",
    ".gif": "image/gif",
    ".webp": "image/webp",
}

VERDICT_SCHEMA = {
    "type": "object",
    "properties": {
        "verdict": {"type": "string", "enum": ["supported", "contradicted", "unverifiable"]},
        "confidence": {"type": "integer"},
        "reasoning": {"type": "string"},
        "basis": {"type": "string"},
    },
    "required": ["verdict", "confidence", "reasoning"],
}

PROMPT = """You are assisting a court clerk with a factual sanity check on a document exhibit.

Question: {question}

Answer only from well-established public knowledge, and follow these rules exactly:
- Answer "contradicted" only when public record clearly shows the combination was
  impossible: the organization did not exist yet, had already dissolved, had not yet
  entered that city/market (for example a utility brand that acquired the local company
  later), or the named place did not exist on that date.
- Answer "supported" only when public record clearly confirms it.
- Answer "unverifiable" for anything else. Small, private, or local businesses are
  almost always unverifiable. Never guess, and never infer from the name alone.
- "confidence" is your confidence in the verdict itself, 0-100.
- "basis" names the kind of public record you relied on, or "none"."""

ANALYSIS_TYPES = (
    "date_inconsistency",
    "math_error",
    "text_overlay",
    "graphic_manipulation",
    "font_mismatch",
    "logical_inconsistency",
    "spelling_anomaly",
    "historical_impossibility",
    "template_fabrication",
)

FRAUD_SCHEMA = {
    "type": "object",
    "properties": {
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "analysis_type": {"type": "string", "enum": list(ANALYSIS_TYPES)},
                    "severity": {"type": "string", "enum": ["info", "low", "medium", "high"]},
                    "confidence": {"type": "integer"},
                    "page": {"type": "integer"},
                    "title": {"type": "string"},
                    "observation": {"type": "string"},
                    "why_suspicious": {"type": "string"},
                    "quoted_evidence": {"type": "string"},
                },
                "required": [
                    "analysis_type",
                    "severity",
                    "confidence",
                    "title",
                    "observation",
                    "why_suspicious",
                ],
            },
        },
        "overall_note": {"type": "string"},
    },
    "required": ["findings"],
}

FRAUD_PROMPT = """Assume this court exhibit may contain fraud or alteration. Aggressively analyze
the attached file and extracted text for ANY signs of document manipulation, forgery, or
inconsistency. Do not give the document the benefit of the doubt.

CRITICAL — multi-statement exhibits:
This PDF may contain several distinct statements (bank, utility, telecom, or similar)
stapled as one exhibit. Review EVERY page and EVERY distinct statement separately.
Do NOT stop after finding issues on one statement. Report every distinct issue on every
statement that has one. An exhibit with three forged statements should produce findings for
all three, not only the most obvious page.

Examine:

1. date_inconsistency — dates that contradict each other or the document's own narrative
   (a signature predating the agreement, a service period outside the billing period, a
   monthly bill whose due date is years after the statement date, a payment month that
   cannot fall in the billing cycle, a sequence that cannot have happened in that order).
   Do NOT treat a demand / default / right-to-cure / acceleration notice as inconsistent
   merely because it recites an older missed installment or default date (for example a
   2023 letter saying the next installment is still due on 02/28/2022). That is how
   lenders show when payments stopped and that arrears remain. A past-due date that
   predates the letter is expected. Only flag if the letter's own dates cannot coexist
   (two different letter dates, a cure deadline before the letter was written, etc.).
2. math_error — arithmetic that does not add up on a bank, utility, telecom, or similar
   account statement: line items that do not sum to the stated subtotal, a tax or discount
   that does not match the stated rate, a balance that is not total minus payments, running
   balances that skip, opening balance plus credits minus debits that do not equal the
   closing balance. State the numbers you used.
   Do NOT flag recorded public instruments (mortgages, deeds, assignments, indentures)
   for rounding, interest computation, tax computation, principal/interest splits, or
   other loan-math quirks. Those records are not bank statements. Only report math on
   them if a figure looks pasted, overwritten, or otherwise altered.
3. text_overlay — text that appears pasted over other content: misaligned baselines,
   different background tone behind a value, a figure or name that sits at a different
   angle or weight than its row, visible patching around an amount.
4. graphic_manipulation — retouching, cloned regions, erased areas, inconsistent noise or
   sharpness, edges of a pasted rectangle, a signature or stamp that looks composited.
5. font_mismatch — a character, number, word, or line set in a different typeface, weight,
   size, or spacing than the surrounding text of the same field.
6. logical_inconsistency — internal contradictions in the document's own content (a party
   named inconsistently, a total described in words that differs from the numeral, terms
   that reference an attachment that is absent).
7. spelling_anomaly — a place name, party name, or branded field that is misspelled in a way
   an authentic issuer statement would not print (letters on the page spell the wrong word,
   not ordinary OCR garbling of a correctly printed word).
8. historical_impossibility — a well-established public fact makes the statement impossible
   as a contemporaneous record: the named issuer did not yet serve that city/market on that
   statement date, the company did not exist yet, or had already exited that market. Use only
   well-established public facts; if unsure, omit rather than guess.
9. template_fabrication — a bought blank (bank statement, utility bill, invoice, pay stub,
   or similar) filled in to help a party, then printed and scanned so it looks like a paper
   original. Look for leftover template chrome (SPECIMEN, SAMPLE, Your Company Name,
   lorem ipsum, [Account Number], example.com), typed-in name/amount/address sitting in a
   different face or alignment than the printed form, a logo that does not match an issuer
   statement from that period, or fields that look dropped into a stock grid. Print-then-scan
   is the concealment step — still report leftover template language and field/chrome mismatch.
   Do not treat an ordinary flatbed scan of a real issuer statement as this. Court filings
   and recorded mortgages/deeds are out of scope.

Rules you must follow:
- Report nothing unless you can cite the specific page and the specific text or region.
  Put the exact words or numbers in "quoted_evidence".
- Name the issuer in the title when the exhibit has more than one statement
  (e.g. "Bank statement math error on page 2", "Utility market timeline on page 4").
- Return an empty "findings" array ONLY when every page and every statement looks ordinary.
  Empty is correct for a clean document; it is incorrect when other statements still have
  unresolved issues.
- Do not flag normal document features: scan noise, JPEG softness, skew from a flatbed,
  letterhead in a display typeface, a handwritten signature differing from printed text,
  rounded currency, or ordinary OCR garbling of a correctly printed word.
- Do NOT flag court e-filing / docket headers. Many courts (including New York NYSCEF)
  print a stamp on every page such as:
  "FILED: … COUNTY CLERK mm/dd/yyyy", "INDEX NO. #####/yyyy", "NYSCEF DOC. NO. …",
  "RECEIVED NYSCEF: mm/dd/yyyy". These are system-generated page furniture.
  Never treat as date_inconsistency, historical_impossibility, text_overlay, or
  font_mismatch any of the following expected mismatches:
  (a) INDEX NO. case year vs FILED / RECEIVED stamp date (case opened earlier than this filing);
  (b) FILED / RECEIVED stamp date vs older dates printed in the exhibit body (bills, letters,
  statements from prior years commonly filed years later);
  (c) the stamp typeface/box differing from the scanned exhibit content underneath.
  Treat FILED / INDEX / NYSCEF / RECEIVED lines as invisible. Never quote them. Never
  compare their dates or the index-number year to any date in the body. If the only
  “inconsistency” you can find uses those header dates, report nothing.
  Only flag dates that contradict each other inside the exhibit's own narrative, ignoring
  the e-filing header lines entirely when comparing chronology.
- Do NOT flag default, breach, right-to-cure, acceleration, or collection letters for
  citing an installment, payment, or default date that is months or years earlier than
  the letter date. Court exhibits routinely prove when the borrower stopped paying.
  "Next payment still due on [older date]" or "you have been in default since [older
  date]" is ordinary arrears language, not a forged chronology.
- "confidence" is 0-100 for that specific observation. Use under 50 when you are unsure.
- You are briefing a busy attorney. Report ONLY stop-the-desk problems: arithmetic that
  does not reconcile, pasted overlays, graphic splicing, leftover form-template language
  or a filled-in bought template that was printed and scanned, or a well-established
  historical impossibility on the exhibit itself. Do not report font mix, spelling, caption
  wording, or other pleading nuances. Do not report the e-filing stamp date (or index year)
  versus any date printed in the exhibit body — that mismatch is how courts file old records.
- "severity" reflects how much the observation would matter if confirmed by a human.
- Be specific in "title" (name the issuer, field, and page), and explain in "why_suspicious"
  why an authentic document would not normally look this way.

Extracted text from the file:
---
{text}
---"""


@dataclass
class Mention:
    text: str
    kind: str
    start: int
    normalized: str | None = None


@dataclass
class Claim:
    organization: str
    location: str | None
    date_iso: str
    date_text: str
    question: str
    page_hint: int | None = None

    def key(self) -> str:
        """Stable across runs so a re-scan produces the same finding IDs for the audit trail."""
        raw = f"{self.organization.lower()}|{(self.location or '').lower()}|{self.date_iso}"
        return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:10]


@dataclass
class Entities:
    organizations: list[Mention] = field(default_factory=list)
    locations: list[Mention] = field(default_factory=list)
    dates: list[Mention] = field(default_factory=list)

    def summary(self) -> dict[str, Any]:
        return {
            "organizations": _unique_texts(self.organizations),
            "locations": _unique_texts(self.locations),
            "dates": sorted({m.normalized for m in self.dates if m.normalized}),
            "counts": {
                "organizations": len(self.organizations),
                "locations": len(self.locations),
                "dates": len(self.dates),
            },
        }


def analyze_contextual(
    text: str,
    doc_meta: dict[str, Any] | None = None,
    source_file: Path | None = None,
    *,
    scan_profile: str = "forensic",
) -> ModuleResult:
    """Entry point matching the other engines: returns a ModuleResult of Findings.

    ``source_file`` is the original exhibit on disk. It is uploaded natively for tier-3
    document analysis and is ignored unless CONTEXTUAL_MULTIMODAL is enabled.
    Court filings skip tier-3 multimodal fraud review; they still extract entities and
    run claim checks for case consistency.
    """
    if not (text or "").strip() and source_file is None:
        return ModuleResult(
            name="contextual_logic",
            status="skipped",
            summary="No extractable text or source file to analyze.",
            extras={"skipped": True, "reason": "no_content"},
        )
    return timed_module(
        "contextual_logic",
        lambda: _run(text, doc_meta or {}, source_file, scan_profile=scan_profile),
    )


def _run(
    text: str,
    doc_meta: dict[str, Any],
    source_file: Path | None,
    *,
    scan_profile: str = "forensic",
) -> tuple[list[Finding], dict[str, Any]]:
    court_filing = scan_profile == "court_filing"
    entities = extract_entities(text)
    # File-creation vs body-date checks are exhibit-authenticity signals; pleadings are
    # routinely e-filed years after the caption date.
    findings = [] if court_filing else check_internal_timeline(entities, doc_meta)
    claims = build_claims(entities)

    extras: dict[str, Any] = {
        "entities": entities.summary(),
        "claims": [asdict(c) for c in claims],
        "verifications": [],
        "external_verification": "disabled",
        "document_analysis": "disabled",
        "provider": None,
        "scan_profile": scan_profile,
        "gemini_usage": {
            "prompt_tokens": 0,
            "candidates_tokens": 0,
            "thoughts_tokens": 0,
            "total_tokens": 0,
            "calls": 0,
        },
    }

    if not settings.gemini_api_key:
        return findings, extras

    extras["provider"] = f"google:{settings.gemini_model}"

    try:
        # Tier 3 multimodal fraud review is for exhibits only.
        if settings.contextual_multimodal and not court_filing:
            document_findings, status = _run_document_analysis(text, source_file, extras)
            findings.extend(document_findings)
            extras["document_analysis"] = status
        elif court_filing:
            extras["document_analysis"] = "skipped_court_filing"
            extras["external_verification"] = "skipped_court_filing"
            return findings, extras
        else:
            extras["document_analysis"] = "disabled"

        if extras["document_analysis"] == "error":
            extras["external_verification"] = "skipped_after_error"
            return findings, extras

        pending_claims = [
            claim
            for claim in claims[: settings.contextual_max_claims]
            if not _claim_covered_by_findings(claim, findings)
        ]
        skipped = len(claims[: settings.contextual_max_claims]) - len(pending_claims)
        if skipped:
            extras["claims_skipped_as_redundant"] = skipped

        if not pending_claims:
            extras["external_verification"] = "no_claims" if not claims else "skipped_redundant"
            return findings, extras

        extras["external_verification"] = "enabled"
        claim_results = _verify_claims_parallel(pending_claims, extras)
        saw_error = False
        for claim, verdict, error in claim_results:
            if error is not None:
                extras["verifications"].append({"question": claim.question, "error": str(error)})
                if not saw_error:
                    findings.append(_verification_unavailable(error))
                    saw_error = True
                continue
            record = {"question": claim.question, **verdict}
            extras["verifications"].append(record)
            contradiction = _finding_from_verdict(claim, verdict)
            if contradiction:
                findings.append(contradiction)

        return findings, extras
    finally:
        _finalize_gemini_usage(extras)


def _claim_covered_by_findings(claim: Claim, findings: list[Finding]) -> bool:
    """Skip tier-2 when tier-3 already flagged the same org for historical impossibility."""
    org = (claim.organization or "").strip().lower()
    if not org:
        return False
    for item in findings:
        evidence = item.evidence or {}
        if not evidence.get("ai_generated"):
            continue
        if evidence.get("analysis_type") != "historical_impossibility":
            continue
        blob = " ".join(
            [
                item.title or "",
                item.explanation or "",
                str(evidence.get("quoted_evidence") or ""),
            ]
        ).lower()
        if org in blob:
            return True
        # Match shortened brand tokens ("national grid" vs "Nationalgrid").
        compact_org = re.sub(r"[^a-z0-9]+", "", org)
        compact_blob = re.sub(r"[^a-z0-9]+", "", blob)
        if compact_org and compact_org in compact_blob:
            return True
    return False


def _verify_claims_parallel(
    claims: list[Claim], extras: dict[str, Any]
) -> list[tuple[Claim, dict[str, Any] | None, Exception | None]]:
    """Run claim checks concurrently; return results in the same order as ``claims``."""
    if not claims:
        return []
    if len(claims) == 1:
        claim = claims[0]
        try:
            verdict = verify_claim(claim.question, extras)
            return [(claim, verdict, None)]
        except Exception as exc:
            return [(claim, None, exc)]

    ordered: list[tuple[Claim, dict[str, Any] | None, Exception | None]] = [
        (claim, None, None) for claim in claims
    ]
    workers = min(len(claims), settings.contextual_max_claims)
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(verify_claim, claim.question, extras): index
            for index, claim in enumerate(claims)
        }
        for future in as_completed(futures):
            index = futures[future]
            claim = claims[index]
            try:
                ordered[index] = (claim, future.result(), None)
            except Exception as exc:
                ordered[index] = (claim, None, exc)
    return ordered


def extract_entities(text: str) -> Entities:
    entities = Entities()
    window = text[: settings.contextual_max_chars]

    for match in ORG_RE.finditer(window):
        name = _clean_org(match.group(1))
        if name:
            entities.organizations.append(Mention(text=name, kind="organization", start=match.start()))

    for match in KNOWN_ISSUER_RE.finditer(window):
        name = _canonical_issuer(match.group(1))
        entities.organizations.append(Mention(text=name, kind="organization", start=match.start()))

    for match in LOCATION_RE.finditer(window):
        city = _clean_city(match.group(1))
        if not city:
            continue
        entities.locations.append(
            Mention(text=f"{city}, {match.group(2)}", kind="location", start=match.start())
        )

    # Scanned utility bills often glue city and state ("BROOKLYNNY") without a comma.
    for match in re.finditer(
        rf"\b(Brooklyn|Queens|Bronx|Manhattan|Staten[ \t]?Island|Aventura|Miami|Dallas|"
        rf"Houston|Chicago|Boston|Philadelphia|Baltimore|Newark)[ \t,]*({STATE_ABBR})\b",
        window,
        re.I,
    ):
        raw_city = re.sub(r"\s+", " ", match.group(1)).strip()
        city = "Staten Island" if raw_city.lower().startswith("staten") else raw_city.title()
        entities.locations.append(
            Mention(text=f"{city}, {match.group(2).upper()}", kind="location", start=match.start())
        )
    for match in re.finditer(r"\b(BROOKLYN)(NY)\b", window, re.I):
        entities.locations.append(
            Mention(text="Brooklyn, NY", kind="location", start=match.start())
        )

    # Filing-stamp dates belong to the court packaging layer, not the exhibit narrative.
    stamp_spans = _court_stamp_date_spans(window)

    seen_spans: set[int] = set()
    for pattern in DATE_RES:
        for match in pattern.finditer(window):
            if match.start() in seen_spans:
                continue
            if _span_overlaps_any(match.start(), match.end(), stamp_spans):
                continue
            iso = _normalize_date(match.group(1))
            if not iso:
                continue
            seen_spans.add(match.start())
            entities.dates.append(
                Mention(text=match.group(1), kind="date", start=match.start(), normalized=iso)
            )

    # Compact statement dates common on utility bills: "Apr 07 '07" / "Apr.16, 2007"
    for match in re.finditer(
        r"\b((?:Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Sept|Oct|Nov|Dec)[a-z]*\.?\s+\d{1,2}"
        r"(?:,|\s+)?\s*(?:'\d{2}|\d{4}|\d{2}))\b",
        window,
        re.I,
    ):
        if match.start() in seen_spans:
            continue
        if _span_overlaps_any(match.start(), match.end(), stamp_spans):
            continue
        iso = _normalize_date(match.group(1))
        if not iso:
            continue
        seen_spans.add(match.start())
        entities.dates.append(
            Mention(text=match.group(1), kind="date", start=match.start(), normalized=iso)
        )

    return entities


def _court_stamp_date_spans(text: str) -> list[tuple[int, int]]:
    """Character spans for FILED / RECEIVED NYSCEF dates (court packaging, not body chronology)."""
    spans: list[tuple[int, int]] = []
    for match in COURT_STAMP_DATE_CONTEXT_RE.finditer(text):
        spans.append((match.start(1), match.end(1)))
    # Near each header token, ignore nearby mm/dd/yyyy stamps (INDEX / FILED / RECEIVED band).
    for header in COURT_FILING_HEADER_RE.finditer(text):
        left = max(0, header.start() - 40)
        right = min(len(text), header.end() + 140)
        window = text[left:right]
        for date_match in re.finditer(r"\b\d{1,2}[/-]\d{1,2}[/-]\d{2,4}\b", window):
            spans.append((left + date_match.start(), left + date_match.end()))
    for match in INDEX_YEAR_RE.finditer(text):
        spans.append((match.start(1), match.end(1)))
    return spans


def _span_overlaps_any(start: int, end: int, spans: list[tuple[int, int]]) -> bool:
    return any(start < span_end and end > span_start for span_start, span_end in spans)


def build_claims(entities: Entities) -> list[Claim]:
    """One claim per organization, so a five-date invoice does not yield five near-identical questions.

    The location is the one nearest that organization's own mention rather than nearest
    the date, which otherwise attaches a party to the counterparty's address. The date is
    the earliest mentioned near any occurrence of the organization, since the earliest date
    is what a not-yet-existing entity would contradict. If no date is nearby (common on
    multi-page scanned exhibits), fall back to the earliest body date in the file.
    """
    by_org: dict[str, list[Mention]] = {}
    for mention in entities.organizations:
        by_org.setdefault(mention.text, []).append(mention)

    claims: list[Claim] = []
    seen: set[str] = set()
    body_dates = [d for d in entities.dates if d.normalized]

    for name, mentions in by_org.items():
        if name.lower() in ORG_STOPWORDS or name.lower() == "account services":
            continue
        anchor = mentions[0]
        location = _nearest(entities.locations, anchor.start)
        if name in NY_MARKET_ISSUERS:
            ny_locs = [loc for loc in entities.locations if loc.text.upper().endswith(", NY")]
            if ny_locs:
                location = _nearest(ny_locs, anchor.start)
            elif location and not location.text.upper().endswith(", NY"):
                # Do not ask whether a NY utility served Aventura, FL.
                location = None
        nearby = [
            d
            for d in body_dates
            if any(abs(d.start - m.start) <= settings.contextual_window * 3 for m in mentions)
        ]
        if not nearby and body_dates:
            nearby = list(body_dates)
        if not nearby:
            continue
        # Prefer statement-era dates over the NYSCEF / clerk filing stamp.
        era = [d for d in nearby if (d.normalized or "") < "2020-01-01"]
        date = min(era or nearby, key=lambda d: d.normalized or "")

        place = location.text if location else None
        if place is None and name in NY_MARKET_ISSUERS:
            brooklyn = next(
                (loc for loc in entities.locations if "brooklyn" in loc.text.lower()),
                None,
            )
            if brooklyn:
                place = brooklyn.text
        if place:
            question = (
                f'Was the organization "{name}" authorized to issue residential utility or '
                f"banking statements for customers at {place} on {date.normalized}? "
                f"In particular, was it operating in that city/market on that date?"
            )
        else:
            question = f'Was the organization "{name}" in operation on {date.normalized}?'

        claim = Claim(
            organization=name,
            location=place,
            date_iso=date.normalized or "",
            date_text=date.text,
            question=question,
        )
        if claim.key() in seen:
            continue
        seen.add(claim.key())
        claims.append(claim)

    return claims


def check_internal_timeline(entities: Entities, doc_meta: dict[str, Any]) -> list[Finding]:
    """Deterministic checks that need no external service."""
    findings: list[Finding] = []
    created = _parse(doc_meta.get("created"))
    body_dates = [d for d in entities.dates if d.normalized]
    if not body_dates:
        return findings

    parsed = [(d, _parse(d.normalized)) for d in body_dates]
    parsed = [(d, value) for d, value in parsed if value]
    if not parsed:
        return findings

    if created:
        # A document cannot legitimately bear a date later than the file that contains it.
        later = [(d, value) for d, value in parsed if value > created + timedelta(days=1)]
        if later:
            worst = max(later, key=lambda item: item[1])
            findings.append(
                finding(
                    fid="ctx-date-after-file",
                    category="contextual",
                    severity="medium",
                    title="Document text is dated after the file itself was created",
                    explanation=(
                        f"The text refers to {worst[0].text}, but the file records a creation date of "
                        f"{created.date().isoformat()}. A genuine record cannot describe events that had "
                        "not happened when the file was written, unless the file was later re-saved."
                    ),
                    why_suspicious=(
                        "Back-dated or re-used templates produce exactly this pattern: an old file "
                        "carrying a newer date in its text."
                    ),
                    confidence=64,
                    evidence={
                        "body_date": worst[0].normalized,
                        "file_created": created.isoformat(),
                        "matched_text": worst[0].text,
                    },
                    recommended_action=(
                        "Ask for the native original and confirm whether the file was edited after the "
                        "date shown in the text."
                    ),
                )
            )

    now = datetime.now(timezone.utc)
    future = [(d, value) for d, value in parsed if value > now]
    if future:
        sample = max(future, key=lambda item: item[1])
        findings.append(
            finding(
                fid="ctx-future-date",
                category="contextual",
                severity="info",
                title="Document text contains a future date",
                explanation=(
                    f"The text refers to {sample[0].text}, which is after today. Forward-dated effective "
                    "dates, maturity dates, and renewal terms are entirely normal in contracts."
                ),
                why_suspicious=(
                    "Only worth attention if the date is presented as something that already happened."
                ),
                confidence=28,
                evidence={"body_date": sample[0].normalized},
            )
        )

    return findings


def _thinking_config(level: str) -> dict[str, Any]:
    """Cap Gemini thinking. Gemini 3.x uses thinkingLevel; 2.5 Flash uses thinkingBudget."""
    level = (level or "minimal").strip().lower()
    if level not in {"minimal", "low", "medium", "high"}:
        level = "minimal"
    model = (settings.gemini_model or "").lower()
    if "2.5" in model:
        # 0 disables thinking on 2.5 Flash when the caller asks for minimal.
        budget = 0 if level == "minimal" else {"low": 512, "medium": 2048, "high": 8192}.get(level, 0)
        return {"thinkingConfig": {"thinkingBudget": budget}}
    return {"thinkingConfig": {"thinkingLevel": level}}


def _generation_config(schema: dict[str, Any], thinking_level: str) -> dict[str, Any]:
    config: dict[str, Any] = {
        "temperature": 0,
        "responseMimeType": "application/json",
        "responseSchema": schema,
    }
    config.update(_thinking_config(thinking_level))
    return config


def _usage_from_body(body: dict[str, Any]) -> dict[str, int]:
    meta = body.get("usageMetadata") or {}
    return {
        "prompt_tokens": int(meta.get("promptTokenCount") or 0),
        "candidates_tokens": int(meta.get("candidatesTokenCount") or 0),
        "thoughts_tokens": int(meta.get("thoughtsTokenCount") or 0),
        "total_tokens": int(meta.get("totalTokenCount") or 0),
    }


def _accumulate_gemini_usage(extras: dict[str, Any] | None, body: dict[str, Any]) -> None:
    if extras is None:
        return
    lock = extras.setdefault("_gemini_usage_lock", threading.Lock())
    usage = _usage_from_body(body)
    with lock:
        bucket = extras.setdefault(
            "gemini_usage",
            {
                "prompt_tokens": 0,
                "candidates_tokens": 0,
                "thoughts_tokens": 0,
                "total_tokens": 0,
                "calls": 0,
            },
        )
        for key, value in usage.items():
            bucket[key] = int(bucket.get(key) or 0) + value
        bucket["calls"] = int(bucket.get("calls") or 0) + 1


def _finalize_gemini_usage(extras: dict[str, Any]) -> None:
    extras.pop("_gemini_usage_lock", None)


def analyze_document(
    text: str,
    file_base64: str | None,
    mime_type: str | None,
    extras: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Tier 3: send the original file and extracted text to Gemini for alteration review.

    This transmits the exhibit's content to a third party. The caller is responsible for
    checking settings.contextual_multimodal before calling.
    """
    parts: list[dict[str, Any]] = [
        {"text": FRAUD_PROMPT.format(text=(text or "")[: settings.contextual_max_chars])}
    ]
    if file_base64 and mime_type:
        parts.append({"inline_data": {"mime_type": mime_type, "data": file_base64}})

    body = _post_gemini(
        {
            "contents": [{"role": "user", "parts": parts}],
            "generationConfig": _generation_config(
                FRAUD_SCHEMA, settings.gemini_thinking_level
            ),
        },
        timeout=settings.gemini_document_timeout_s,
    )
    _accumulate_gemini_usage(extras, body)
    parsed = _parse_json_payload(body)
    raw_findings = parsed.get("findings")
    if not isinstance(raw_findings, list):
        raw_findings = []
    return {
        "findings": raw_findings,
        "overall_note": str(parsed.get("overall_note", ""))[:500],
        "file_sent": mime_type if file_base64 else None,
        "model": settings.gemini_model,
    }


def _run_document_analysis(
    text: str, source_file: Path | None, extras: dict[str, Any]
) -> tuple[list[Finding], str]:
    """Encode the exhibit for native upload, then hand it to analyze_document.

    A format Gemini cannot read natively, or a file too large for an inline request,
    degrades to text-only analysis rather than failing the module.
    """
    file_base64, mime_type, note = _encode_source_file(source_file)
    if note:
        extras["document_analysis_note"] = note

    if file_base64 is None and not (text or "").strip():
        return [], "no_content"
    try:
        result = analyze_document(text, file_base64, mime_type, extras)
    except Exception as exc:
        extras["document_analysis_error"] = str(exc)
        return [_verification_unavailable(exc, stage="document analysis")], "error"

    extras["document_analysis_result"] = {
        "count": len(result["findings"]),
        "file_sent": result["file_sent"],
        "overall_note": result["overall_note"],
    }
    findings: list[Finding] = []
    for index, item in enumerate(result["findings"][: settings.contextual_max_ai_findings]):
        converted = _finding_from_analysis(item, index, result, text=text, source_file=source_file)
        if converted:
            findings.append(converted)
    return findings, "enabled"


def _encode_source_file(source_file: Path | None) -> tuple[str | None, str | None, str | None]:
    """Base64-encode the exhibit for native upload.

    Returns (data, mime_type, note). Any of the first two being None means the file is
    not being sent and the note explains why, for the record in extras.
    """
    if source_file is None:
        return None, None, None
    suffix = source_file.suffix.lower()
    mime_type = NATIVE_MIME_TYPES.get(suffix)
    if mime_type is None:
        return None, None, f"{suffix or 'this format'} has no native Gemini equivalent; analyzed text only."
    try:
        size = source_file.stat().st_size
        limit = settings.contextual_max_upload_mb * 1024 * 1024
        if size > limit:
            return (
                None,
                None,
                f"File is {size / 1048576:.1f} MB, over the {settings.contextual_max_upload_mb} MB "
                "inline upload limit; analyzed text only.",
            )
        with open(source_file, "rb") as handle:
            return base64.b64encode(handle.read()).decode("utf-8"), mime_type, None
    except OSError as exc:
        return None, None, f"Could not read the file for upload: {exc}"


def _post_gemini(payload: dict[str, Any], timeout: int | None = None) -> dict[str, Any]:
    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    request = urllib.request.Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-goog-api-key": settings.gemini_api_key or "",
        },
    )
    try:
        with urllib.request.urlopen(
            request, timeout=timeout or settings.gemini_timeout_s
        ) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Gemini: {exc.reason}") from exc


def _parse_json_payload(body: dict[str, Any]) -> dict[str, Any]:
    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(part.get("text", "") for part in parts).strip()
    if not raw:
        raise RuntimeError("Gemini returned an empty response.")
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Gemini returned unparsable JSON: {exc}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError("Gemini returned JSON that was not an object.")
    return parsed


def verify_claim(question: str, extras: dict[str, Any] | None = None) -> dict[str, Any]:
    """Tier 2: ask Gemini to adjudicate one claim. Sends the question only, no document content."""
    body = _post_gemini(
        {
            "contents": [{"role": "user", "parts": [{"text": PROMPT.format(question=question)}]}],
            "generationConfig": _generation_config(
                VERDICT_SCHEMA, settings.gemini_claim_thinking_level
            ),
        }
    )
    _accumulate_gemini_usage(extras, body)
    parsed = _parse_json_payload(body)

    verdict = str(parsed.get("verdict", "unverifiable")).lower()
    if verdict not in {"supported", "contradicted", "unverifiable"}:
        verdict = "unverifiable"
    try:
        model_confidence = float(parsed.get("confidence", 0))
    except (TypeError, ValueError):
        model_confidence = 0.0
    return {
        "verdict": verdict,
        "model_confidence": max(0.0, min(100.0, model_confidence)),
        "reasoning": str(parsed.get("reasoning", ""))[:600],
        "basis": str(parsed.get("basis", ""))[:200],
        "model": settings.gemini_model,
    }


ANALYSIS_TITLES = {
    "date_inconsistency": "Date inconsistency",
    "math_error": "Arithmetic does not reconcile",
    "text_overlay": "Possible text overlay",
    "graphic_manipulation": "Possible graphic manipulation",
    "font_mismatch": "Font or type mismatch",
    "logical_inconsistency": "Internal inconsistency",
    "spelling_anomaly": "Spelling or name anomaly",
    "historical_impossibility": "Historical / market timeline conflict",
    "template_fabrication": "Possible bought-template exhibit",
}

# The model may propose any severity; nothing it reports is allowed above medium.
SEVERITY_CAP = {"info": "info", "low": "low", "medium": "medium", "high": "medium", "critical": "medium"}


def _finding_from_analysis(
    item: dict[str, Any],
    index: int,
    result: dict[str, Any],
    text: str = "",
    source_file: Path | None = None,
) -> Finding | None:
    """Convert one model-reported observation into a Finding, discounted and labelled.

    Category stays "contextual" for every analysis type. A model's opinion about splicing
    is not equivalent to the deterministic copy-move detector's output, so it must not be
    filed under "image" where a reader would take it for measured evidence.
    """
    if not isinstance(item, dict):
        return None
    analysis_type = str(item.get("analysis_type", "")).strip().lower()
    if analysis_type not in ANALYSIS_TYPES:
        return None
    if analysis_type in {"font_mismatch", "logical_inconsistency", "spelling_anomaly"}:
        return None

    observation = str(item.get("observation", "")).strip()
    why = str(item.get("why_suspicious", "")).strip()
    if not observation or not why:
        return None

    try:
        model_confidence = float(item.get("confidence", 0))
    except (TypeError, ValueError):
        model_confidence = 0.0
    model_confidence = max(0.0, min(100.0, model_confidence))
    if model_confidence < settings.contextual_min_confidence:
        return None

    severity = SEVERITY_CAP.get(str(item.get("severity", "low")).lower(), "low")
    # Same discount as claim verification: an unconfirmed model observation cannot
    # reach the >=70 threshold that scoring.py requires for red.
    confidence = min(60.0, model_confidence * 0.7)

    page = item.get("page")
    try:
        page_no = int(page) if page is not None else None
    except (TypeError, ValueError):
        page_no = None
    if page_no is not None and page_no < 1:
        page_no = None

    quoted = str(item.get("quoted_evidence", "")).strip()[:300]
    draft = {
        "title": str(item.get("title") or ""),
        "observation": observation,
        "quoted_evidence": quoted,
        "analysis_type": analysis_type,
    }
    if is_header_or_arrears_noise(draft):
        return None
    source_name = str(source_file) if source_file is not None else ""
    if is_recorded_instrument_math_noise(draft, text=text, source=source_name):
        return None

    label = ANALYSIS_TITLES[analysis_type]
    title = str(item.get("title", "")).strip()[:120] or label

    explanation = f"Automated document review reported: {observation}"
    if quoted:
        explanation += f' Cited content: "{quoted}"'

    fid_seed = f"{analysis_type}|{title}|{page_no}|{quoted}"
    fid = f"ctx-ai-{hashlib.sha1(fid_seed.encode('utf-8')).hexdigest()[:10]}"

    return finding(
        fid=fid,
        category="contextual",
        severity=severity,
        title=f"{label}: {title}" if not title.lower().startswith(label.lower()) else title,
        explanation=explanation,
        why_suspicious=why,
        confidence=confidence,
        page=page_no,
        evidence={
            "analysis_type": analysis_type,
            "quoted_evidence": quoted or None,
            "model": result.get("model"),
            "model_confidence": model_confidence,
            "model_severity": str(item.get("severity", "")).lower() or None,
            "file_sent": result.get("file_sent"),
            "ai_generated": True,
        },
        recommended_action=(
            "Treat this as a research lead only. Confirm the cited page and content by eye, and "
            "compare against the deterministic findings from the image, text, and PDF modules."
        ),
    )


def _finding_from_verdict(claim: Claim, verdict: dict[str, Any]) -> Finding | None:
    if verdict["verdict"] != "contradicted":
        return None
    if verdict["model_confidence"] < settings.contextual_min_confidence:
        return None

    # Capped: a model assertion is a research lead, never proof, so it cannot drive
    # the report to red on its own.
    confidence = min(60.0, verdict["model_confidence"] * 0.7)
    where = f" at {claim.location}" if claim.location else ""
    return finding(
        fid=f"ctx-contradiction-{claim.key()}",
        category="contextual",
        severity="medium",
        title=f"Public record may contradict “{claim.organization}” on {claim.date_iso}",
        explanation=(
            f"An automated public-knowledge check questioned whether {claim.organization} was "
            f"operating{where} on {claim.date_iso}. The model reported: {verdict['reasoning']}"
        ),
        why_suspicious=(
            "If the named party did not exist or was not operating on the stated date, the document "
            "cannot be a contemporaneous record of that transaction."
        ),
        confidence=confidence,
        evidence={
            "question": claim.question,
            "verdict": verdict["verdict"],
            "model": verdict["model"],
            "model_confidence": verdict["model_confidence"],
            "basis": verdict.get("basis"),
            "ai_generated": True,
        },
        recommended_action=(
            "Treat this as a research lead only. Confirm against the Secretary of State registry or "
            "another authoritative source before relying on it."
        ),
    )


def _verification_unavailable(exc: Exception, stage: str = "fact verification") -> Finding:
    return finding(
        fid=f"ctx-unavailable-{re.sub(r'[^a-z]+', '-', stage.lower())}",
        category="contextual",
        severity="info",
        title=f"External {stage} did not run",
        explanation=(
            f"The contextual verification service could not be reached ({exc}). Entity extraction and "
            "internal timeline checks still ran, and all other engines are unaffected."
        ),
        why_suspicious="Not an authenticity issue; it records a gap in coverage for the audit trail.",
        confidence=0,
    )


def _canonical_issuer(raw: str) -> str:
    lowered = re.sub(r"\s+", " ", raw).strip().lower()
    aliases = {
        "nationalgrid": "National Grid",
        "national grid": "National Grid",
        "citi bank": "Citibank",
        "citibank": "Citibank",
        "verizon": "Verizon",
        "keyspan": "KeySpan",
        "con edison": "Con Edison",
        "bank of america": "Bank of America",
        "wells fargo": "Wells Fargo",
        "capital one": "Capital One",
        "jp morgan": "JPMorgan",
        "jpmorgan": "JPMorgan",
        "at&t": "AT&T",
        "t-mobile": "T-Mobile",
    }
    return aliases.get(lowered, raw.strip().title())


def _clean_org(raw: str) -> str | None:
    tokens = re.sub(r"\s+", " ", raw).strip(" ,;:").split(" ")
    tokens = _drop_through_marker(tokens, LABEL_MARKERS)
    tokens = _drop_leading_shouts(tokens)
    name = " ".join(tokens).strip(" ,;:")
    if len(name) < 5 or len(name) > 90:
        return None
    lowered = name.lower()
    if any(lowered.startswith(word) for word in ORG_STOPWORDS):
        return None
    if lowered in ORG_STOPWORDS:
        return None
    # Counsel letterhead is ubiquitous on pleadings; do not treat it as a case party/issuer.
    if re.search(r"\b(pllc|l\.l\.p\.?|llp)\b", lowered) and not re.search(
        r"\b(bank|trust|national\s+grid|edison|verizon|citibank)\b", lowered
    ):
        return None
    if re.search(r"\b(attorney|attorneys|law\s+office|law\s+firm|counsel)\b", lowered):
        return None
    if not re.search(r"[a-z]", name):  # all-caps headers like "TERMS AND CO"
        return None
    return name


def _clean_city(raw: str) -> str | None:
    tokens = [t for t in re.sub(r"\s+", " ", raw).strip().split(" ") if not t.isdigit()]
    tokens = _drop_through_marker(tokens, LABEL_MARKERS | STREET_MARKERS | ORG_MARKERS)
    if len(tokens) > 3:  # longest real US city names are three words
        tokens = tokens[-3:]
    city = " ".join(tokens).strip(" ,;:")
    if len(city) < 3 or city.lower() in ORG_STOPWORDS:
        return None
    return city


def _drop_through_marker(tokens: list[str], markers: set[str]) -> list[str]:
    """Keep only what follows the last marker token, e.g. '1400 Commerce Street Dallas' -> 'Dallas'."""
    last = -1
    for index, token in enumerate(tokens):
        if _bare(token) in markers:
            last = index
    return tokens[last + 1 :] if last >= 0 else tokens


def _drop_leading_shouts(tokens: list[str]) -> list[str]:
    """Strip an all-caps heading glued to a title-case name, keeping wholly all-caps names intact."""
    if not any(token[:1].isupper() and any(c.islower() for c in token) for token in tokens):
        return tokens
    index = 0
    while index < len(tokens) - 1:
        token = _bare(tokens[index])
        if len(token) >= 3 and token.isalpha() and tokens[index].isupper():
            index += 1
            continue
        break
    return tokens[index:]


def _bare(token: str) -> str:
    return re.sub(r"[^A-Za-z]", "", token).lower()


def _unique_texts(mentions: list[Mention]) -> list[str]:
    seen: dict[str, None] = {}
    for mention in mentions:
        seen.setdefault(mention.text, None)
    return list(seen)


def _nearest(mentions: list[Mention], position: int) -> Mention | None:
    candidates = [m for m in mentions if abs(m.start - position) <= settings.contextual_window]
    if not candidates:
        return None
    return min(candidates, key=lambda m: abs(m.start - position))


def _normalize_date(raw: str) -> str | None:
    parsed = _parse(raw)
    if parsed is None:
        return None
    now = datetime.now(timezone.utc)
    if parsed.year < 1900 or parsed.year > now.year + 25:
        return None
    # Reject parses that silently dropped an explicit 4-digit year in the source text
    # (dateutil turns "Nov 30,2017" into the current year's Nov 30).
    year_match = re.search(r"(?<!\d)(\d{4})(?!\d)", str(raw))
    if year_match and int(year_match.group(1)) != parsed.year:
        return None
    return parsed.date().isoformat()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value).strip()
    # OCR often omits the space after a month-day comma ("Nov 30,2017").
    text = re.sub(
        r"\b((?:January|February|March|April|May|June|July|August|September|October|"
        r"November|December|[A-Za-z]{3,9})\s+\d{1,2}),(\d{4})\b",
        r"\1, \2",
        text,
        flags=re.I,
    )
    try:
        parsed = dateparser.parse(text, fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
