from __future__ import annotations

import re
from collections import Counter
from typing import TypedDict

from app.finding_policy import forgery_risk_band, is_material_finding
from app.gotcha import TYPE_SCORE_WEIGHT
from app.models import Finding, RiskLevel

CRITICAL_RISK_LABEL = "Critical risk — strong indicators of alteration"
VISUAL_REVIEW_INCOMPLETE_LABEL = "Visual review did not complete — not cleared"
RISK_LABEL = {
    "green": "Low risk",
    "yellow": "Moderate risk",
    "red": "High risk",
    "incomplete": VISUAL_REVIEW_INCOMPLETE_LABEL,
}

# Clean-copy phrases a finished visual review may use. An incomplete required
# review must not use them.
_CLEAN_REVIEW_ACTION = (
    "No high-confidence manipulation indicators were found. This does not prove authenticity; "
    "it means the automated checks did not raise a material alarm."
)


SEVERITY_WEIGHT = {
    "info": 0,
    "low": 6,
    "medium": 18,
    "high": 36,
    "critical": 55,
}

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def risk_level_from_score(score: int | None) -> RiskLevel:
    """Map numeric forensic score to a label. Score 0 is never High."""
    try:
        n = int(score or 0)
    except (TypeError, ValueError):
        n = 0
    if n >= 70:
        return "red"
    if n >= 35:
        return "yellow"
    return "green"


def score_risk(findings: list[Finding]) -> tuple[RiskLevel, int]:
    """Score from verified findings only — never document type or title.

    Two affidavits (or two exhibits) must score differently if one has verified
    gotchas and the other does not. Procedural and admission gotchas weigh more
    than a minor factual inconsistency.
    """
    risk, score, _drivers = score_risk_details(findings)
    return risk, score


def score_risk_details(
    findings: list[Finding],
) -> tuple[RiskLevel, int, list[dict[str, object]]]:
    """Return score, level, and score_drivers.

    An exhibit that appears forged floors at 100. Clean checks are not averaged
    in and cannot pull that floor back down.
    """
    actionable = [
        f
        for f in findings
        if f.severity != "info" and f.confidence >= 40 and is_material_finding(f)
    ]
    raw = 0
    for item in actionable:
        weight = SEVERITY_WEIGHT[item.severity] * (item.confidence / 100.0)
        if item.category == "gotcha":
            gotcha_type = str((item.evidence or {}).get("contradiction_type") or "")
            weight *= TYPE_SCORE_WEIGHT.get(gotcha_type, 1.15)
            if (item.evidence or {}).get("tainted_support"):
                weight *= 1.5
            elif (item.evidence or {}).get("self_undercut"):
                weight *= 1.2
            elif (item.evidence or {}).get("evidence_tier") == 1 and (item.evidence or {}).get("claim_tier") == 1:
                weight *= 1.1
        raw += int(weight)
    weighted = min(100, raw)
    band, floor, critical = forgery_risk_band(findings)
    # Floor is a minimum. Additional clean checks cannot reduce it.
    score = min(100, max(weighted, floor))
    drivers: list[dict[str, object]] = [
        {
            "type": "weighted_findings",
            "raw_score": weighted,
            "finding_count": len(actionable),
            "explanation": "Sum of severity weights adjusted by confidence and verified finding type.",
        }
    ]
    if floor:
        drivers.append(
            {
                "type": "critical_floor",
                "floor": floor,
                "band": band,
                "critical_count": len(critical),
                "score": score,
                "explanation": (
                    "An exhibit that appears forged floors the score at 100. "
                    "Clean checks do not lower that floor."
                ),
            }
        )
    level = risk_level_from_score(score)
    drivers.append(
        {
            "type": "level_from_score",
            "score": score,
            "level": level,
            "explanation": "Risk label follows the numeric score (green <35, yellow <70, red >=70).",
        }
    )
    return level, score, drivers


def risk_banner(level: str, findings: list[Finding]) -> str:
    """Attorney-facing label. Forged exhibits read as critical risk.

    An incomplete required visual review is not low risk and is not a forgery finding.
    """
    if level == "incomplete":
        return VISUAL_REVIEW_INCOMPLETE_LABEL
    band, _floor, _critical = forgery_risk_band(findings)
    if band == "critical":
        return CRITICAL_RISK_LABEL
    return RISK_LABEL.get(level, level)


def pick_top_findings(findings: list[Finding], limit: int = 3) -> list[Finding]:
    material = [f for f in findings if is_material_finding(f)]
    pool = material or [f for f in findings if f.severity != "info"]
    fraud_types = {
        "math_error",
        "historical_impossibility",
        "template_fabrication",
        "logo_chrome_mismatch",
        "date_inconsistency",
        "line_self_contradiction",
    }
    ranked = sorted(
        pool,
        key=lambda f: (
            1 if (f.evidence or {}).get("tainted_support") else 0,
            1 if (f.evidence or {}).get("case_impact") == "case_winning" else 0,
            1 if (f.evidence or {}).get("analysis_type") in fraud_types else 0,
            1 if (f.evidence or {}).get("analysis_type") == "date_inconsistency" else 0,
            SEVERITY_RANK[f.severity],
            f.confidence,
        ),
        reverse=True,
    )
    return ranked[:limit]


def recommended_actions(
    risk: RiskLevel,
    findings: list[Finding],
    *,
    visual_review_incomplete: bool = False,
) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    uncleared = risk == "incomplete" or (visual_review_incomplete and risk == "green")
    if not uncleared:
        for item in pick_top_findings(findings, 8):
            if item.recommended_action and item.recommended_action not in seen:
                seen.add(item.recommended_action)
                actions.append(item.recommended_action)
    if uncleared:
        actions.append(
            "Visual review did not complete. Do not treat this exhibit as cleared. "
            "Re-run the scan when the visual review can finish."
        )
    elif risk == "red":
        actions.append(
            "Do not treat this exhibit as verified. Flag it for the assigned attorney or clerk and keep this report with the case file."
        )
    elif risk == "yellow":
        actions.append(
            "Review the highlighted findings before relying on this exhibit. Request a native original if the file appears exported or rebuilt."
        )
    else:
        actions.append(_CLEAN_REVIEW_ACTION)
    if visual_review_incomplete and risk in ("yellow", "red"):
        actions.append("Visual review did not complete, so this result does not clear the file.")
    # Deduplicate while preserving order
    unique: list[str] = []
    for item in actions:
        if item not in unique:
            unique.append(item)
    return unique[:6]


def _critical_clause(item: Finding) -> str:
    page = f"page {item.page}" if item.page else "page not stated"
    quote = str((item.evidence or {}).get("quoted_evidence") or "").strip()
    evidence = quote or re.sub(r"\s+", " ", item.explanation or "").strip()
    if len(evidence) > 280:
        evidence = evidence[:277].rstrip() + "..."
    return f"{page} — {item.title}. Evidence: {evidence}"


def _incomplete_visual_summary(filename: str) -> str:
    return (
        f"Visual review of “{filename}” did not complete, so this file was not cleared. "
        "This is not a low-risk result. No fraud finding is recorded from a review that did not finish."
    )


def _local_only_green_summary(filename: str) -> str:
    return (
        f"Local-only review of “{filename}” found no material alarm in the checks that ran on this computer. "
        "The visual model pass was not run. This is a local-only result, not a completed visual review."
    )


def executive_summary(
    risk: RiskLevel,
    filename: str,
    findings: list[Finding],
    *,
    visual_review_incomplete: bool = False,
    review_mode: str | None = None,
) -> str:
    if risk == "incomplete" or (visual_review_incomplete and risk == "green"):
        return _incomplete_visual_summary(filename)
    counts = Counter(f.severity for f in findings if is_material_finding(f))
    top = pick_top_findings(findings)
    band, _floor, critical = forgery_risk_band(findings)
    def _with_review_note(text: str) -> str:
        if review_mode == "local_only":
            text += " This is a local-only review; the visual model pass was not run."
        if visual_review_incomplete:
            text += " Visual review did not complete, so this result does not clear the file."
        return text

    if review_mode == "local_only" and risk == "green" and not critical:
        return _local_only_green_summary(filename)
    if risk == "green" and not critical:
        return (
            f"Automated review of “{filename}” did not find strong signs of digital alteration. "
            "Timestamps, structure, and image-forensics checks are consistent with a routine file. "
            "Staff should still apply ordinary evidentiary judgment."
        )
    if band == "critical" and critical:
        listed = " ".join(
            f"({index}) {_critical_clause(item)}" for index, item in enumerate(critical, start=1)
        )
        return _with_review_note(
            f"{CRITICAL_RISK_LABEL}. “{filename}” {listed} "
            "Review each cited page against the scan. These are indicators for counsel to check."
        )
    lead = top[0].title if top else "inconsistent internal signals"
    if risk == "red" or band == "high":
        extra = ""
        if critical:
            extra = " " + " ".join(_critical_clause(item) for item in critical)
        return _with_review_note(
            f"“{filename}” is assessed as high risk. The leading concern is: {lead}.{extra} "
            f"The scan recorded {counts.get('critical', 0)} critical, {counts.get('high', 0)} high, "
            f"and {counts.get('medium', 0)} medium finding(s). Treat the exhibit as unverified until reviewed."
        )
    text = (
        f"“{filename}” is assessed as moderate risk. The leading concern is: {lead}. "
        "One or more checks found inconsistencies that deserve human review before the exhibit is relied on."
    )
    return _with_review_note(text)


class ReviewPresentation(TypedDict):
    risk_level: RiskLevel
    risk_score: int
    score_drivers: list[dict[str, object]]
    executive_summary: str
    recommended_actions: list[str]


def apply_review_presentation(
    findings: list[Finding],
    filename: str,
    *,
    ai_review: str | None,
    review_mode: str | None = None,
) -> ReviewPresentation:
    """Score findings, then refuse a low-risk clearance when visual review did not succeed.

    Real yellow/red findings stay. A green result becomes ``incomplete`` and
    does not use the routine-file summary. Court-filing and local-only reviews
    pass ``ai_review`` other than ``incomplete`` and keep their own copy.
    """
    level, score, drivers = score_risk_details(findings)
    incomplete = ai_review == "incomplete"
    if incomplete and level == "green":
        level = "incomplete"
        drivers = [
            *drivers,
            {
                "type": "visual_review_incomplete",
                "score": score,
                "level": "incomplete",
                "explanation": (
                    "Required visual review did not return a successful response. "
                    "The file is not cleared as low risk."
                ),
            },
        ]
    return {
        "risk_level": level,
        "risk_score": score,
        "score_drivers": drivers,
        "executive_summary": executive_summary(
            level,
            filename,
            findings,
            visual_review_incomplete=incomplete,
            review_mode=review_mode,
        ),
        "recommended_actions": recommended_actions(
            level,
            findings,
            visual_review_incomplete=incomplete,
        ),
    }
