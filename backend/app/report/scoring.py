from __future__ import annotations

from collections import Counter

from app.finding_policy import is_material_finding
from app.gotcha import TYPE_SCORE_WEIGHT
from app.models import Finding, Report, RiskLevel


SEVERITY_WEIGHT = {
    "info": 0,
    "low": 6,
    "medium": 18,
    "high": 36,
    "critical": 55,
}

SEVERITY_RANK = {"info": 0, "low": 1, "medium": 2, "high": 3, "critical": 4}


def score_risk(findings: list[Finding]) -> tuple[RiskLevel, int]:
    """Score from verified findings only — never document type or title.

    Two affidavits (or two exhibits) must score differently if one has verified
    gotchas and the other does not. Procedural and admission gotchas weigh more
    than a minor factual inconsistency.
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
            if (item.evidence or {}).get("self_undercut"):
                weight *= 1.2
            elif (item.evidence or {}).get("evidence_tier") == 1 and (item.evidence or {}).get("claim_tier") == 1:
                weight *= 1.1
        raw += int(weight)
    score = min(100, raw)

    if any(f.severity == "critical" and f.confidence >= 70 for f in actionable):
        return "red", max(score, 80)
    if any(f.severity == "high" and f.confidence >= 70 for f in actionable):
        return "red", max(score, 70)
    if any(f.severity in {"medium", "high"} and f.confidence >= 55 for f in actionable):
        return "yellow", max(score, 35)
    return "green", score


def pick_top_findings(findings: list[Finding], limit: int = 3) -> list[Finding]:
    material = [f for f in findings if is_material_finding(f)]
    pool = material or [f for f in findings if f.severity != "info"]
    ranked = sorted(
        pool,
        key=lambda f: (SEVERITY_RANK[f.severity], f.confidence),
        reverse=True,
    )
    return ranked[:limit]


def recommended_actions(risk: RiskLevel, findings: list[Finding]) -> list[str]:
    actions: list[str] = []
    seen: set[str] = set()
    for item in pick_top_findings(findings, 8):
        if item.recommended_action and item.recommended_action not in seen:
            seen.add(item.recommended_action)
            actions.append(item.recommended_action)
    if risk == "red":
        actions.append(
            "Do not treat this exhibit as verified. Flag it for the assigned attorney or clerk and keep this report with the case file."
        )
    elif risk == "yellow":
        actions.append(
            "Review the highlighted findings before relying on this exhibit. Request a native original if the file appears exported or rebuilt."
        )
    else:
        actions.append(
            "No high-confidence manipulation indicators were found. This does not prove authenticity; it means the automated checks did not raise a material alarm."
        )
    # Deduplicate while preserving order
    unique: list[str] = []
    for item in actions:
        if item not in unique:
            unique.append(item)
    return unique[:6]


def executive_summary(risk: RiskLevel, filename: str, findings: list[Finding]) -> str:
    counts = Counter(f.severity for f in findings if is_material_finding(f))
    top = pick_top_findings(findings)
    if risk == "green":
        return (
            f"Automated review of “{filename}” did not find strong signs of digital alteration. "
            "Timestamps, structure, and image-forensics checks are consistent with a routine file. "
            "Staff should still apply ordinary evidentiary judgment."
        )
    lead = top[0].title if top else "inconsistent internal signals"
    if risk == "red":
        return (
            f"“{filename}” is assessed as high risk. The leading concern is: {lead}. "
            f"The scan recorded {counts.get('critical', 0)} critical, {counts.get('high', 0)} high, "
            f"and {counts.get('medium', 0)} medium finding(s). Treat the exhibit as unverified until reviewed."
        )
    return (
        f"“{filename}” is assessed as moderate risk. The leading concern is: {lead}. "
        "One or more checks found inconsistencies that deserve human review but are not, by themselves, "
        "proof of fraud."
    )
