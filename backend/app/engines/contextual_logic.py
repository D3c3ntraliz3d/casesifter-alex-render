"""Contextual fraud detection.

Extracts entities (organizations, dates, locations) from an exhibit and checks
whether the story the document tells is internally possible, then optionally asks
Google Gemini whether a stated combination of company, place, and date is
contradicted by public record.

Two constraints shape this module:

1. Privacy. Only the generated question is sent to Google — never page text,
   never the file. External verification stays off unless GEMINI_API_KEY is set.
2. Evidentiary weight. A language model's factual claim is a research lead, not
   proof. Model-sourced findings are capped at medium severity and carry
   ai_generated=True so the report can label them for the reader.
"""

from __future__ import annotations

import hashlib
import json
import re
import urllib.error
import urllib.request
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any

from dateutil import parser as dateparser

from app.config import settings
from app.engines import finding, timed_module
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
    re.compile(
        r"\b((?:January|February|March|April|May|June|July|August|September|October|"
        r"November|December)\s+\d{1,2},?\s+\d{4})\b",
        re.I,
    ),
    re.compile(
        r"\b(\d{1,2}\s+(?:January|February|March|April|May|June|July|August|September|"
        r"October|November|December),?\s+\d{4})\b",
        re.I,
    ),
)

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
  impossible: the organization did not exist yet, had already dissolved, or the
  named place did not exist on that date.
- Answer "supported" only when public record clearly confirms it.
- Answer "unverifiable" for anything else. Small, private, or local businesses are
  almost always unverifiable. Never guess, and never infer from the name alone.
- "confidence" is your confidence in the verdict itself, 0-100.
- "basis" names the kind of public record you relied on, or "none"."""


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


def analyze_contextual(text: str, doc_meta: dict[str, Any] | None = None) -> ModuleResult:
    """Entry point matching the other engines: returns a ModuleResult of Findings."""
    if not (text or "").strip():
        return ModuleResult(
            name="contextual_logic",
            status="skipped",
            summary="No extractable text to analyze.",
            extras={"skipped": True, "reason": "no_text"},
        )
    return timed_module("contextual_logic", lambda: _run(text, doc_meta or {}))


def _run(text: str, doc_meta: dict[str, Any]) -> tuple[list[Finding], dict[str, Any]]:
    entities = extract_entities(text)
    findings = check_internal_timeline(entities, doc_meta)
    claims = build_claims(entities)

    extras: dict[str, Any] = {
        "entities": entities.summary(),
        "claims": [asdict(c) for c in claims],
        "verifications": [],
        "external_verification": "disabled",
        "provider": None,
    }

    if not settings.gemini_api_key or not claims:
        extras["external_verification"] = "disabled" if not settings.gemini_api_key else "no_claims"
        return findings, extras

    extras["external_verification"] = "enabled"
    extras["provider"] = f"google:{settings.gemini_model}"

    for claim in claims[: settings.contextual_max_claims]:
        try:
            verdict = verify_claim(claim.question)
        except Exception as exc:
            extras["verifications"].append({"question": claim.question, "error": str(exc)})
            findings.append(_verification_unavailable(exc))
            break

        record = {"question": claim.question, **verdict}
        extras["verifications"].append(record)
        contradiction = _finding_from_verdict(claim, verdict)
        if contradiction:
            findings.append(contradiction)

    return findings, extras


def extract_entities(text: str) -> Entities:
    entities = Entities()
    window = text[: settings.contextual_max_chars]

    for match in ORG_RE.finditer(window):
        name = _clean_org(match.group(1))
        if name:
            entities.organizations.append(Mention(text=name, kind="organization", start=match.start()))

    for match in LOCATION_RE.finditer(window):
        city = _clean_city(match.group(1))
        if not city:
            continue
        entities.locations.append(
            Mention(text=f"{city}, {match.group(2)}", kind="location", start=match.start())
        )

    seen_spans: set[int] = set()
    for pattern in DATE_RES:
        for match in pattern.finditer(window):
            if match.start() in seen_spans:
                continue
            iso = _normalize_date(match.group(1))
            if not iso:
                continue
            seen_spans.add(match.start())
            entities.dates.append(
                Mention(text=match.group(1), kind="date", start=match.start(), normalized=iso)
            )

    return entities


def build_claims(entities: Entities) -> list[Claim]:
    """One claim per organization, so a five-date invoice does not yield five near-identical questions.

    The location is the one nearest that organization's own mention rather than nearest
    the date, which otherwise attaches a party to the counterparty's address. The date is
    the earliest mentioned near any occurrence of the organization, since the earliest date
    is what a not-yet-existing entity would contradict.
    """
    by_org: dict[str, list[Mention]] = {}
    for mention in entities.organizations:
        by_org.setdefault(mention.text, []).append(mention)

    claims: list[Claim] = []
    seen: set[str] = set()

    for name, mentions in by_org.items():
        anchor = mentions[0]
        location = _nearest(entities.locations, anchor.start)
        nearby = [
            d
            for d in entities.dates
            if d.normalized
            and any(abs(d.start - m.start) <= settings.contextual_window for m in mentions)
        ]
        if not nearby:
            continue
        date = min(nearby, key=lambda d: d.normalized or "")

        place = location.text if location else None
        if place:
            question = (
                f'Was the organization "{name}" in operation, and present at '
                f"{place}, on {date.normalized}?"
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


def verify_claim(question: str) -> dict[str, Any]:
    """Ask Gemini to adjudicate one claim. Sends the question only, never document text."""
    url = GEMINI_ENDPOINT.format(model=settings.gemini_model)
    payload = {
        "contents": [{"role": "user", "parts": [{"text": PROMPT.format(question=question)}]}],
        "generationConfig": {
            "temperature": 0,
            "responseMimeType": "application/json",
            "responseSchema": VERDICT_SCHEMA,
        },
    }
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
        with urllib.request.urlopen(request, timeout=settings.gemini_timeout_s) as response:
            body = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")[:300]
        raise RuntimeError(f"Gemini returned HTTP {exc.code}: {detail}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Could not reach Gemini: {exc.reason}") from exc

    candidates = body.get("candidates") or []
    if not candidates:
        raise RuntimeError("Gemini returned no candidates.")
    parts = (candidates[0].get("content") or {}).get("parts") or []
    raw = "".join(part.get("text", "") for part in parts).strip()
    if not raw:
        raise RuntimeError("Gemini returned an empty response.")
    parsed = json.loads(raw)

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


def _verification_unavailable(exc: Exception) -> Finding:
    return finding(
        fid="ctx-verification-unavailable",
        category="contextual",
        severity="info",
        title="External fact verification did not run",
        explanation=(
            f"The contextual verification service could not be reached ({exc}). Entity extraction and "
            "internal timeline checks still ran."
        ),
        why_suspicious="Not an authenticity issue; it records a gap in coverage for the audit trail.",
        confidence=0,
    )


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
    return parsed.date().isoformat()


def _parse(value: Any) -> datetime | None:
    if not value:
        return None
    try:
        parsed = dateparser.parse(str(value), fuzzy=False)
    except (ValueError, OverflowError, TypeError):
        return None
    if parsed is None:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed
