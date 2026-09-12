"""Keep attorney-facing risk on major issues only.

Court e-filing headers and ordinary pleading packaging must not turn a file yellow.
Moderate/high risk is reserved for the class of problems already proven useful:
arithmetic that does not reconcile, pasted overlays, graphic splicing, and
well-established historical impossibilities on the exhibit itself.
"""

from __future__ import annotations

import re

from app.models import Finding

HEADER_NOISE_RE = re.compile(
    r"(?i)("
    r"\b(?:NYSCEF|INDEX\s+NO\.?|FILED:|RECEIVED\s+NYSCEF|COUNTY\s+CLERK|"
    r"DOC(?:UMENT)?\.?\s*NO\.?)\b|"
    r"\be-?fil(?:ed|ing)\b|"
    r"\bdocket\s+(?:stamp|header|caption|date)\b|"
    r"\bclerk'?s?\s+(?:stamp|endorsement|file\s+stamp)\b|"
    r"\be-?filing\s+header\b|"
    r"\bcourt\s+fil(?:ed|ing)\s+date\b|"
    r"\bindex\s+(?:number|year)\b"
    r")"
)

# Gemini often restates "letter is 2017 / this file was filed in 2024" without saying NYSCEF.
FILING_VS_BODY_RE = re.compile(
    r"(?i)("
    r"(?:fil(?:ed|ing)\s+date|e-?fil(?:ed|ing)|index\s+(?:no|number|year)|nyscef|"
    r"docket\s+date|clerk(?:'s)?\s+stamp).{0,160}"
    r"(?:dated|letter|statement|invoice|bill|exhibit|body|content|written|issued)"
    r"|"
    r"(?:dated|letter\s+date|statement\s+date|invoice\s+date|exhibit).{0,160}"
    r"(?:fil(?:ed|ing)\s+date|e-?fil(?:ed|ing)|nyscef|index\s+no|clerk(?:'s)?\s+stamp)"
    r"|"
    r"(?:pdf|file|document)\s+(?:was\s+)?(?:created|modified|filed|uploaded|scanned).{0,80}"
    r"(?:dated|letter|statement|invoice)"
    r")"
)

# Demand / default letters citing an older missed installment.
ARREARS_OK_RE = re.compile(
    r"(?i)(still\s+due|in\s+default\s+since|right\s+to\s+cure|arrears|"
    r"installment\s+payment\s+is\s+still|missed\s+payment|past[\s-]due)"
)

RECORDED_KINDS = frozenset({"mortgage", "deed", "assignment", "satisfaction", "indenture"})
RECORDED_PLEADING_NAME = re.compile(r"(?i)\b(notice|foreclosure|complaint|summons|motion)\b")
RECORDED_NAME = re.compile(
    r"(?i)\b(mortgage|deed|indenture|assignment\s+of\s+mortgage|satisfaction\s+of\s+mortgage)\b"
)
RECORDED_BODY = re.compile(
    r"(?i)("
    r"recorded\s+in\s+(?:the\s+)?(?:office|city\s+register)|"
    r"office\s+of\s+the\s+(?:city\s+register|county\s+clerk)|"
    r"\bcrfn\b|"
    r"block\s+\d+[,\s]+lot\s+\d+|"
    r"mortgage\s+tax|"
    r"this\s+indenture|"
    r"county\s+clerk'?s?\s+office|"
    r"to\s+be\s+recorded\s+in"
    r")"
)

MAJOR_AI_TYPES = frozenset(
    {
        "math_error",
        "text_overlay",
        "graphic_manipulation",
        "historical_impossibility",
        # Body-vs-body chronology only. Header/filing comparisons are dropped first.
        "date_inconsistency",
        "template_fabrication",
    }
)

# Model observations that are almost always pleading/scan nuance, not a stop-the-desk flag.
NUANCE_AI_TYPES = frozenset(
    {
        "font_mismatch",
        "logical_inconsistency",
        "spelling_anomaly",
    }
)

# Deterministic / packaging findings that should never drive yellow or red.
PACKAGING_IDS = frozenset(
    {
        "md-year-gap",
        "md-stripped",
        "md-fs-before-create",
        "md-date-order",
        "md-office-date-order",
        "md-office-core-missing",
        "md-office-empty",
        "st-page-size",
        "st-rotation",
        "st-resolution-mix",
        "pdf-incremental",
        "pdf-javascript",
        "pdf-acroform",
        "pdf-form",
        "pdf-image-only",
        "pdf-hybrid",
        "txt-micro-text",
        "txt-date-vs-file",
        "txt-font-mix",
        "ctx-date-after-file",
        "ctx-future-date",
        "img-lowdpi",
        "img-crop",
    }
)


def finding_blob(item: Finding | dict) -> str:
    if isinstance(item, Finding):
        evidence = item.evidence or {}
        return " ".join(
            [
                item.title or "",
                item.explanation or "",
                item.why_suspicious or "",
                str(evidence.get("quoted_evidence") or ""),
            ]
        )
    evidence = item.get("evidence") or {}
    return " ".join(
        [
            str(item.get("title") or ""),
            str(item.get("observation") or ""),
            str(item.get("explanation") or ""),
            str(item.get("quoted_evidence") or evidence.get("quoted_evidence") or ""),
        ]
    )


def looks_like_recorded_instrument(
    text: str = "",
    source: str = "",
    doc_kind: str | None = None,
) -> bool:
    """True for a recorded mortgage, deed, or similar public instrument."""
    if (doc_kind or "").strip().lower() in RECORDED_KINDS:
        return True
    name = str(source or "")
    if RECORDED_NAME.search(name) and not RECORDED_PLEADING_NAME.search(name):
        return True
    return bool(RECORDED_BODY.search(text or ""))


def is_recorded_instrument_math_noise(
    item: Finding | dict,
    *,
    text: str = "",
    source: str = "",
    doc_kind: str | None = None,
) -> bool:
    """Loan-math on a recorded instrument is not a finding; alteration still is."""
    analysis = ""
    if isinstance(item, Finding):
        analysis = str((item.evidence or {}).get("analysis_type") or "")
        name = source or str(item.title or "")
    else:
        analysis = str(item.get("analysis_type") or "")
        name = source or str(item.get("title") or "")
    if analysis != "math_error":
        return False
    blob = finding_blob(item)
    return looks_like_recorded_instrument(text, name, doc_kind) or bool(RECORDED_BODY.search(blob))


def is_header_or_arrears_noise(item: Finding | dict) -> bool:
    blob = finding_blob(item)
    if HEADER_NOISE_RE.search(blob):
        return True
    analysis = ""
    if isinstance(item, Finding):
        analysis = str((item.evidence or {}).get("analysis_type") or "")
    else:
        analysis = str(item.get("analysis_type") or "")
    if analysis in NUANCE_AI_TYPES:
        return True
    if analysis == "date_inconsistency" and (
        ARREARS_OK_RE.search(blob) or FILING_VS_BODY_RE.search(blob)
    ):
        return True
    if is_recorded_instrument_math_noise(item):
        return True
    return False


def drop_noise_findings(findings: list[Finding]) -> list[Finding]:
    kept: list[Finding] = []
    for item in findings:
        if is_header_or_arrears_noise(item):
            continue
        kept.append(item)
    return kept


def is_material_finding(item: Finding) -> bool:
    """True when an attorney should be interrupted."""
    if item.severity == "info":
        return False
    if is_header_or_arrears_noise(item):
        return False
    if item.id in PACKAGING_IDS or any(item.id.startswith(prefix) for prefix in PACKAGING_IDS):
        return False

    evidence = item.evidence or {}
    analysis = str(evidence.get("analysis_type") or "")
    if analysis == "template_fabrication" and item.severity in {"medium", "high", "critical"}:
        return True
    if analysis == "date_inconsistency" and item.severity in {"medium", "high", "critical"}:
        return True
    if evidence.get("ai_generated"):
        return analysis in MAJOR_AI_TYPES
    if item.category == "gotcha":
        if evidence.get("evidence_tier") == 2:
            return False
        return bool(evidence.get("gotcha") and evidence.get("verified")) and item.severity in {
            "medium",
            "high",
            "critical",
        }
    if item.category == "case_consistency":
        # Cross-document labeled facts are one case-level alert, not N file scores.
        if evidence.get("prior_document_id") and str(evidence.get("field") or "") != "missing_exhibit":
            return False
        return bool(evidence.get("material")) and item.severity in {"medium", "high", "critical"}
    if item.id.startswith("img-ela-"):
        return item.severity in {"medium", "high"} and not evidence.get("scan_heavy")
    if item.id.startswith("img-copymove-"):
        return item.severity in {"medium", "high"} and not evidence.get("scan_heavy")
    if item.id == "md-editor-software":
        return True
    if item.id == "pdf-javascript" and item.severity in {"medium", "high"}:
        return True
    return False


def triage_findings(findings: list[Finding], scan_profile: str) -> list[Finding]:
    """Drop header/nuance noise. Non-material leftovers become info so they cannot yellow a file."""
    del scan_profile  # reserved: court vs exhibit already encoded in is_material_finding
    cleaned = drop_noise_findings(findings)
    triaged: list[Finding] = []
    for item in cleaned:
        if is_material_finding(item) or item.severity == "info":
            triaged.append(item)
            continue
        if item.category == "case_consistency" and item.severity == "low":
            triaged.append(item)
            continue
        triaged.append(
            item.model_copy(
                update={
                    "severity": "info",
                    "confidence": min(item.confidence, 35.0),
                }
            )
        )
    return triaged
