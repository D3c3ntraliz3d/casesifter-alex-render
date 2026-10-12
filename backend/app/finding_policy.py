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
        "logo_chrome_mismatch",
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


# Legitimate filing redactions, not concealment. 22 NYCRR §202.5(e) is the
# New York rule that requires confidential personal information to be redacted.
COURT_REDACTION_RE = re.compile(
    r"(?i)("
    r"redacted\s+per\s+22\s*nycrr|"
    r"22\s*nycrr\s*(?:§|sec(?:tion)?\.?)?\s*202\.5|"
    r"redacted\s+pursuant\s+to"
    r")"
)

# Temporal impossibility: a due date or period that cannot exist on this statement.
TEMPORAL_IMPOSSIBILITY_RE = re.compile(
    r"(?i)("
    r"cannot coexist|"
    r"cannot belong|"
    r"cannot fall|"
    r"precedes its own|"
    r"starts after it ends|"
    r"years\s+(?:after|before|apart|later|earlier)|"
    r"due date.{0,80}(?:cannot|impossible|years)"
    r")"
)

# Font or text-layer mismatch inside an amount, date, or name field.
FIELD_FONT_RE = re.compile(
    r"(?i)("
    r"\b(?:amount|balance|total|payment|fee|sum)\b.{0,60}"
    r"\b(?:font|typeface|text\s+layer|type\s+style)\b"
    r"|"
    r"\b(?:font|typeface|text\s+layer|type\s+style)\b.{0,60}"
    r"\b(?:amount|balance|total|payment|fee)\b"
    r"|"
    r"\b(?:due\s+date|statement\s+date|billing\s+date|invoice\s+date|"
    r"account\s+name|customer\s+name|payee|name\s+field)\b.{0,60}"
    r"\b(?:font|typeface|text\s+layer|type\s+style|mismatch)\b"
    r"|"
    r"\b(?:font|typeface|text\s+layer)\b.{0,60}"
    r"\b(?:due\s+date|statement\s+date|billing\s+date|account\s+name|"
    r"customer\s+name|name\s+field)\b"
    r")"
)


def _analysis_type(item: Finding | dict) -> str:
    if isinstance(item, Finding):
        return str((item.evidence or {}).get("analysis_type") or "")
    return str(item.get("analysis_type") or (item.get("evidence") or {}).get("analysis_type") or "")


def _search_blob(item: Finding | dict) -> str:
    extra = ""
    if isinstance(item, Finding):
        extra = item.why_suspicious or ""
    elif isinstance(item, dict):
        extra = str(item.get("why_suspicious") or "")
    return f"{finding_blob(item)} {extra}"


def is_field_scoped_font_mismatch(item: Finding | dict) -> bool:
    """Mismatched type or text layer inside an amount, date, or name field."""
    blob = _search_blob(item)
    if HEADER_NOISE_RE.search(blob):
        return False
    analysis = _analysis_type(item)
    looks_like_font = analysis == "font_mismatch" or bool(
        re.search(r"(?i)text\s+layer|typeface|\bfont\b", blob)
    )
    if not looks_like_font:
        return False
    return bool(FIELD_FONT_RE.search(blob))


def is_concealment_overlay(item: Finding | dict) -> bool:
    """White box or redaction-style overlay hiding original text."""
    if isinstance(item, Finding) and str(item.id).startswith("img-ela-"):
        return False
    if isinstance(item, Finding) and str(item.id).startswith("img-solid-overlay-"):
        return True
    analysis = _analysis_type(item)
    if analysis == "text_overlay":
        return True
    blob = _search_blob(item).lower()
    if any(token in blob for token in ("white overlay", "whiteout", "white-out", "solid white")):
        return analysis in {"", "graphic_manipulation", "text_overlay"}
    return False


def is_legitimate_court_redaction(text: str) -> bool:
    return bool(COURT_REDACTION_RE.search(text or ""))


def is_temporal_impossibility(item: Finding | dict) -> bool:
    if isinstance(item, Finding) and str(item.id).startswith("txt-stmtdate-"):
        return True
    if _analysis_type(item) != "date_inconsistency":
        return False
    return bool(TEMPORAL_IMPOSSIBILITY_RE.search(_search_blob(item)))


def is_critical_forgery(item: Finding | dict, *, page_text: str = "") -> bool:
    """Strong forgery indicators. These set the risk floor by themselves.

    Legitimate court redactions are expected and are not concealment.
    """
    if is_concealment_overlay(item):
        blob = _search_blob(item)
        if is_legitimate_court_redaction(blob) or is_legitimate_court_redaction(page_text):
            return False
        return True
    if is_temporal_impossibility(item):
        return True
    if _analysis_type(item) == "historical_impossibility":
        return True
    if _analysis_type(item) == "math_error" and not is_recorded_instrument_math_noise(item):
        return True
    if is_field_scoped_font_mismatch(item):
        return True
    return False


def text_for_page(
    full_text: str,
    page: int | None,
    page_offsets: list[dict] | None = None,
) -> str:
    """Page text for the redaction exclusion. Empty when the page cannot be isolated."""
    body = full_text or ""
    if page and page_offsets:
        starts = sorted(page_offsets, key=lambda entry: int(entry.get("start") or 0))
        for index, entry in enumerate(starts):
            if int(entry.get("page") or 0) != int(page):
                continue
            start = int(entry.get("start") or 0)
            end = int(starts[index + 1]["start"]) if index + 1 < len(starts) else len(body)
            return body[start:end]
    if page and "\f" in body:
        parts = body.split("\f")
        if 1 <= int(page) <= len(parts):
            return parts[int(page) - 1]
    if page == 1 and "\f" not in body and not page_offsets:
        return body
    return ""


def apply_forgery_severity(
    findings: list[Finding],
    full_text: str = "",
    page_offsets: list[dict] | None = None,
) -> list[Finding]:
    """Promote strong forgery indicators to critical. Court redactions stay informational."""
    out: list[Finding] = []
    for item in findings:
        page_text = text_for_page(full_text, item.page, page_offsets)
        if is_concealment_overlay(item) and (
            is_legitimate_court_redaction(_search_blob(item))
            or is_legitimate_court_redaction(page_text)
        ):
            evidence = dict(item.evidence or {})
            evidence["legitimate_court_redaction"] = True
            evidence["case_impact"] = "noise"
            out.append(
                item.model_copy(
                    update={
                        "severity": "info",
                        "confidence": min(item.confidence, 35.0),
                        "evidence": evidence,
                    }
                )
            )
            continue
        if item.severity != "info" and is_critical_forgery(item, page_text=page_text):
            if item.severity != "critical":
                out.append(item.model_copy(update={"severity": "critical"}))
            else:
                out.append(item)
            continue
        out.append(item)
    return out


def forgery_risk_band(findings: list[Finding]) -> tuple[str | None, int, list[Finding]]:
    """Return (band, floor, critical findings).

    band is "critical" (floor 95), "high" (floor 85), or None.
    Clean checks are not in this set and cannot lower the floor.
    """
    actionable = [
        item
        for item in findings
        if item.severity != "info" and item.confidence >= 40 and is_material_finding(item)
    ]
    critical: list[Finding] = []
    other_high = 0
    for item in actionable:
        if is_critical_forgery(item, page_text=""):
            critical.append(item)
        elif item.severity == "high":
            other_high += 1
    keys = {
        (
            str((item.evidence or {}).get("analysis_type") or item.id),
            item.page,
        )
        for item in critical
    }
    if len(keys) >= 2 or (critical and other_high):
        return "critical", 95, critical
    if critical:
        return "high", 85, critical
    return None, 0, []


def _deterministic_due_date_impossibility(item: Finding | dict) -> bool:
    """Body due date the statement-date checker already accepted.

    A nearby NYSCEF line or the words "past due" are ordinary on a utility bill.
    They must not erase a due date that cannot belong to the statement date.
    """
    if not isinstance(item, Finding):
        return False
    if not str(item.id).startswith("txt-stmtdate-"):
        return False
    return "due date" in (item.title or "").lower()


def is_header_or_arrears_noise(item: Finding | dict) -> bool:
    if _deterministic_due_date_impossibility(item):
        return False
    blob = finding_blob(item)
    if HEADER_NOISE_RE.search(blob):
        return True
    analysis = ""
    if isinstance(item, Finding):
        analysis = str((item.evidence or {}).get("analysis_type") or "")
    else:
        analysis = str(item.get("analysis_type") or "")
    # Field-level font / text-layer mismatches are forgery indicators, not nuance.
    if is_field_scoped_font_mismatch(item):
        return False
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
        # Exhibit letter/number labels change between initial filing and the
        # recorded packet — "Referenced Exhibit A is not in this case" is noise.
        if str((item.evidence or {}).get("field") or "") == "missing_exhibit":
            continue
        kept.append(item)
    return kept


def is_material_finding(item: Finding) -> bool:
    """True when an attorney should be interrupted."""
    if item.severity == "info":
        return False
    evidence = item.evidence or {}
    if evidence.get("case_impact") == "noise":
        return False
    if is_header_or_arrears_noise(item):
        return False
    if item.id in PACKAGING_IDS or any(item.id.startswith(prefix) for prefix in PACKAGING_IDS):
        return False

    analysis = str(evidence.get("analysis_type") or "")
    if analysis in {"text_overlay", "graphic_manipulation"} and evidence.get("scan_heavy"):
        return False
    # Deterministic whiteout / solid paste on a scanned statement (not AI-capped).
    if item.id.startswith("img-solid-overlay-"):
        return item.severity in {"medium", "high", "critical"} and bool(
            evidence.get("print_scan_review") or evidence.get("content_hook")
        )
    if (
        analysis == "graphic_manipulation"
        and evidence.get("print_scan_review")
        and item.severity in {"medium", "high", "critical"}
    ):
        return True
    if analysis == "logo_chrome_mismatch":
        # Issuer alone is not a content hook — Gemini often names the brand while
        # hallucinating a mismatch on clean decoy statements.
        quoted = str(evidence.get("quoted_evidence") or "").strip()
        logo_note = str(evidence.get("logo_note") or evidence.get("crispness_note") or "").strip()
        if len(quoted) < 12 and len(logo_note) < 12:
            return False
        return item.severity in {"medium", "high", "critical"}
    if analysis == "date_inconsistency" and item.severity in {"medium", "high", "critical"}:
        return True
    if is_field_scoped_font_mismatch(item) and item.severity in {"medium", "high", "critical"}:
        return True
    if analysis == "math_error":
        # Require an arithmetic quote (digits) so clean synthetics without broken math stay quiet.
        quoted = str(evidence.get("quoted_evidence") or item.explanation or "")
        if not re.search(r"\d", quoted):
            return False
        return item.severity in {"medium", "high", "critical"}
    if evidence.get("ai_generated"):
        return analysis in MAJOR_AI_TYPES
    if analysis == "template_fabrication" and item.severity in {"medium", "high", "critical"}:
        return True
    if analysis in {"historical_impossibility", "template_fabrication"} and item.severity in {
        "medium",
        "high",
        "critical",
    }:
        return True
    if item.category == "gotcha":
        if evidence.get("evidence_tier") == 2:
            return False
        return bool(evidence.get("gotcha") and evidence.get("verified")) and item.severity in {
            "medium",
            "high",
            "critical",
        }
    if item.category == "case_consistency":
        # Exhibit letter mismatches are not file scores. Documentary graph conflicts
        # (occupancy_start, presence-on-date, etc.) stay material — otherwise triage
        # demotes them to info and every exhibit risks green while the case board
        # alone holds the interrupt (desktop ephemeral scans never even reach the board).
        if str(evidence.get("field") or "") == "missing_exhibit":
            return False
        if (
            evidence.get("documentary")
            and evidence.get("case_impact")
            and item.severity in {"medium", "high", "critical"}
        ):
            return True
        if evidence.get("prior_document_id"):
            return False
        return bool(evidence.get("material")) and item.severity in {"medium", "high", "critical"}
    if item.id.startswith("img-ela-"):
        # Paper scans: whole-page texture is noise. Only print-scan statement review interrupts.
        return bool(evidence.get("print_scan_review")) and item.severity in {"medium", "high"}
    if item.id.startswith("img-copymove-"):
        return item.severity in {"medium", "high"} and not evidence.get("scan_heavy")
    if item.id == "md-editor-software":
        # Scanner / e-filing producer tags are not proof the paper was forged.
        return False
    if item.id == "pdf-javascript" and item.severity in {"medium", "high"}:
        return True
    return False


def triage_findings(
    findings: list[Finding],
    scan_profile: str,
    full_text: str = "",
    page_offsets: list[dict] | None = None,
) -> list[Finding]:
    """Drop header/nuance noise. Non-material leftovers become info so they cannot yellow a file."""
    del scan_profile  # reserved: court vs exhibit already encoded in is_material_finding
    prepared = apply_forgery_severity(findings, full_text, page_offsets)
    cleaned = drop_noise_findings(prepared)
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
