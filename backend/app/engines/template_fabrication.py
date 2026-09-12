"""Exhibit-only signals that a bought form was filled, printed, and scanned.

Court filings never run through this. Recorded mortgages/deeds are not statements.
"""

from __future__ import annotations

import hashlib
import re

from app.engines import finding
from app.finding_policy import looks_like_recorded_instrument
from app.models import Finding
from app.scan_profile import PROFILE_FORENSIC, STATEMENT_HINT_RE

STRONG_LEFTOVER = re.compile(
    r"(?i)("
    r"\bspecimen\b|"
    r"lorem\s+ipsum|"
    r"your\s+company\s+name|"
    r"company\s+name\s+here|"
    r"your\s+name\s+here|"
    r"insert\s+(?:name|amount|address|account)|"
    r"\[(?:account\s+number|customer\s+name|amount\s+due)\]|"
    r"editable\s+template|"
    r"(?:canva|envato|etsy)\.com|"
    r"www\.example\.com|"
    r"example@email\.com"
    r")"
)
WEAK_LEFTOVER = re.compile(
    r"(?i)("
    r"(?<!a\s)(?<!the\s)\bsample\b(?!\s+of)|"
    r"\bdemo\s+(?:statement|bill|account|invoice)\b|"
    r"\bdummy\s+(?:account|data|statement)\b|"
    r"\btemplate\s+(?:download|editable|invoice|statement)\b|"
    r"\bxxx-xx-xxxx\b|"
    r"555-01\d{2}"
    r")"
)


def looks_like_consumer_statement(text: str) -> bool:
    return bool(STATEMENT_HINT_RE.search(text or ""))


def find_template_leftovers(
    text: str,
    *,
    scan_profile: str,
    source: str = "",
    doc_kind: str | None = None,
) -> list[Finding]:
    """Leftover chrome from a store-bought form. Exhibits only."""
    if scan_profile != PROFILE_FORENSIC:
        return []
    if looks_like_recorded_instrument(text, source, doc_kind):
        return []
    body = text or ""
    if len(body) < 40:
        return []
    strong = list(STRONG_LEFTOVER.finditer(body))
    weak = list(WEAK_LEFTOVER.finditer(body))
    if not strong and len(weak) < 2:
        return []
    hit = (strong[0] if strong else weak[0]).group(0).strip()
    start = body.lower().find(hit.lower())
    snippet = re.sub(r"\s+", " ", body[max(0, start - 40) : start + len(hit) + 80]).strip()[:220]
    seed = f"template|{hit}|{snippet[:80]}"
    fid = "txt-template-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return [
        finding(
            fid=fid,
            category="text",
            severity="high" if strong else "medium",
            title="Leftover form-template language on an exhibit",
            explanation=(
                f"The pulled text still contains form-template chrome (“{hit}”). "
                "Bought bank/utility/invoice templates are often filled in, printed, "
                "and scanned; leftover placeholders survive that flattening."
            ),
            why_suspicious=(
                "An authentic issuer statement is generated as one job. It does not "
                "print specimen labels, 'your company name', lorem ipsum, or other "
                "editable-template leftovers."
            ),
            confidence=86.0 if strong else 72.0,
            evidence={
                "analysis_type": "template_fabrication",
                "quoted_evidence": snippet or hit,
                "template_hit": hit,
            },
            recommended_action=(
                "Compare the chrome (logo, grid, footer) to a known-authentic issuer "
                "statement from the same period. Typed-in name, address, and amount "
                "fields are the usual fill-ins on a bought template."
            ),
        )
    ]


def review_print_scan_ela(
    findings: list[Finding],
    text: str,
    *,
    scan_profile: str,
) -> list[Finding]:
    """Print-then-scan is the concealment step. Do not bury strong localized ELA on statements."""
    if scan_profile != PROFILE_FORENSIC:
        return findings
    leftovers = bool(find_template_leftovers(text, scan_profile=scan_profile))
    statement = looks_like_consumer_statement(text)
    if not leftovers and not statement:
        return findings
    out: list[Finding] = []
    for item in findings:
        evidence = dict(item.evidence or {})
        if not str(item.id).startswith("img-ela-") or not evidence.get("scan_heavy"):
            out.append(item)
            continue
        try:
            score = float(evidence.get("ela_score") or 0)
            blob = float(evidence.get("largest_blob_px") or 0)
        except (TypeError, ValueError):
            out.append(item)
            continue
        strong_ela = score >= 40 and blob >= 1000
        if not leftovers and not strong_ela:
            out.append(item)
            continue
        evidence.pop("scan_heavy", None)
        evidence["print_scan_review"] = True
        evidence["analysis_type"] = evidence.get("analysis_type") or "graphic_manipulation"
        out.append(
            item.model_copy(
                update={
                    "severity": "medium",
                    "confidence": max(item.confidence, 68.0),
                    "title": "Localized recompression on a scanned statement",
                    "why_suspicious": (
                        "A bought template filled in, printed, and scanned often leaves a "
                        "different JPEG history on the typed fields than on the form chrome. "
                        "On a consumer statement that is not ordinary flatbed texture."
                    ),
                    "evidence": evidence,
                }
            )
        )
    return out
