"""Case-impact judge: Claude re-weighs red/yellow findings for usable interrupts.

Does not invent new pairs. Demotes token collisions, he-said-she-said, and
boilerplate overlap to noise so document risk only reflects material findings.
"""

from __future__ import annotations

import json
import urllib.error
import urllib.request
from typing import Any

from app.config import settings
from app.finding_policy import is_material_finding
from app.models import Finding

IMPACT_CASE_WINNING = "case_winning"
IMPACT_MATERIAL = "material_review"
IMPACT_NOISE = "noise"

FRAUD_ANALYSIS = frozenset(
    {
        "math_error",
        "historical_impossibility",
        "template_fabrication",
        "logo_chrome_mismatch",
        "date_inconsistency",
        "text_overlay",
        "graphic_manipulation",
    }
)

JUDGE_PROMPT = """You are a case-impact filter for CaseSifter — systematic pattern detection,
not advocacy. For each finding, decide whether it would actually interrupt a case
(element, procedure, credibility, or damages) or is noise.

Ship only when ALL hold:
1. Same nameable fact (not shared English/legal boilerplate tokens like
   "United States", "made", "person", party names alone, or caption words)
2. Evidence runs AGAINST the claim (not restating or supporting it)
3. Hits: element / procedure / credibility / damages
4. Not bare denial vs opposing recital (he-said-she-said / ordinary traverse)
5. Prefer party-offered exhibit fraud or that party's own exhibit undercutting them

When uncertain, choose "noise". Never invent new contradictions. Never write a motion.

Findings:
{findings}

Return ONLY JSON:
{{"results":[{{"index":0,"impact":"case_winning|material_review|noise","bucket":"element|procedure|credibility|damages|null","rationale":"one sentence"}}]}}
"""


def apply_case_impact_judge(findings: list[Finding]) -> tuple[list[Finding], dict[str, Any]]:
    """Annotate and demote findings. Returns updated list + summary stats."""
    candidates = [
        (idx, item)
        for idx, item in enumerate(findings)
        if _should_judge(item)
    ]
    if not candidates:
        return findings, _summary(findings)

    # Deterministic demotions first (no API): clear boilerplate / already-info.
    local_map: dict[int, dict[str, Any]] = {}
    need_model: list[tuple[int, Finding]] = []
    for idx, item in candidates:
        local = _local_impact(item)
        if local:
            local_map[idx] = local
        else:
            need_model.append((idx, item))

    model_map = _ask_claude_judge(need_model) if need_model else {}

    out: list[Finding] = []
    for idx, item in enumerate(findings):
        verdict = local_map.get(idx) or model_map.get(idx)
        if not verdict:
            out.append(item)
            continue
        out.append(_apply_verdict(item, verdict))
    return out, _summary(out)


def case_impact_summary(findings: list[Finding] | list[dict[str, Any]]) -> dict[str, Any]:
    """Compact board/API payload: material interrupts vs collapsed noise."""
    material: list[dict[str, Any]] = []
    noise: list[dict[str, Any]] = []
    fraud: list[dict[str, Any]] = []
    for item in findings:
        if isinstance(item, Finding):
            ev = item.evidence or {}
            row = {
                "id": item.id,
                "title": item.title,
                "severity": item.severity,
                "impact": ev.get("case_impact") or _default_impact(item),
                "bucket": ev.get("case_impact_bucket"),
                "rationale": ev.get("case_impact_rationale") or "",
                "tainted_support": bool(ev.get("tainted_support")),
                "analysis_type": ev.get("analysis_type"),
                "category": item.category,
            }
            material_flag = is_material_finding(item) and item.severity != "info"
        else:
            ev = item if isinstance(item, dict) else {}
            row = {
                "id": ev.get("id"),
                "title": ev.get("title"),
                "severity": ev.get("severity"),
                "impact": ev.get("case_impact") or IMPACT_NOISE,
                "bucket": ev.get("case_impact_bucket"),
                "rationale": ev.get("case_impact_rationale") or "",
                "tainted_support": bool(ev.get("tainted_support")),
                "analysis_type": ev.get("analysis_type"),
                "category": ev.get("category"),
            }
            material_flag = (
                row["impact"] in {IMPACT_CASE_WINNING, IMPACT_MATERIAL}
                and str(row["severity"] or "") not in {"info", "low", ""}
            )
        if row["tainted_support"] or row["analysis_type"] in FRAUD_ANALYSIS:
            if material_flag:
                fraud.append(row)
        if material_flag:
            material.append(row)
        elif row["impact"] == IMPACT_NOISE or str(row["severity"]) == "info":
            noise.append(row)
    # Fraud exhibits first in material ordering for UI.
    ordered = sorted(
        material,
        key=lambda r: (
            0 if r.get("tainted_support") or r.get("analysis_type") in FRAUD_ANALYSIS else 1,
            0 if r.get("impact") == IMPACT_CASE_WINNING else 1,
        ),
    )
    return {
        "material_count": len(ordered),
        "noise_count": len(noise),
        "fraud_count": len(fraud),
        "material": ordered[:12],
        "noise_collapsed": len(noise),
        "noise_samples": noise[:5],
    }


def _should_judge(item: Finding) -> bool:
    if item.severity not in {"medium", "high", "critical"}:
        return False
    ev = item.evidence or {}
    if item.category == "gotcha" or ev.get("gotcha"):
        return True
    if ev.get("analysis_type") in FRAUD_ANALYSIS:
        return True
    if ev.get("tainted_support"):
        return True
    return False


def _local_impact(item: Finding) -> dict[str, Any] | None:
    """Deterministic rules; return None to defer to Claude."""
    ev = item.evidence or {}
    if ev.get("tainted_support") or ev.get("analysis_type") in FRAUD_ANALYSIS:
        # Standalone exhibit fraud / tainted support is always material when severity shipped.
        bucket = "credibility"
        if ev.get("analysis_type") in {"math_error"}:
            bucket = "damages"
        elif ev.get("analysis_type") in {"date_inconsistency", "historical_impossibility"}:
            bucket = "credibility"
        return {
            "impact": IMPACT_CASE_WINNING if item.severity in {"high", "critical"} else IMPACT_MATERIAL,
            "bucket": bucket,
            "rationale": "Scan-survivable exhibit fraud or altered offered record.",
            "gate": "local_fraud",
        }
    claim = str(ev.get("claim_quote") or "")
    evidence = str(ev.get("evidence_quote") or "")
    fact = str(ev.get("fact_named") or "")
    from app.gotcha import _boilerplate_collision, _ordinary_traverse

    if _boilerplate_collision(claim, evidence, fact):
        return {
            "impact": IMPACT_NOISE,
            "bucket": None,
            "rationale": "Shared boilerplate tokens, not the same nameable fact.",
            "gate": "local_boilerplate",
        }
    if _ordinary_traverse(claim, evidence):
        return {
            "impact": IMPACT_NOISE,
            "bucket": None,
            "rationale": "Ordinary traverse / he-said-she-said, not a gotcha.",
            "gate": "local_traverse",
        }
    return None


def _ask_claude_judge(items: list[tuple[int, Finding]]) -> dict[int, dict[str, Any]]:
    key = (settings.anthropic_api_key or "").strip()
    if not key or not items:
        # Without Claude, keep existing material findings; demote only when local rules fired.
        return {
            idx: {
                "impact": IMPACT_MATERIAL,
                "bucket": (item.evidence or {}).get("materiality_reason") and "element",
                "rationale": "Kept pending model judge (no API key).",
                "gate": "no_api",
            }
            for idx, item in items
        }
    blob = "\n\n".join(
        f"[{i}] category={item.category} severity={item.severity} title={item.title}\n"
        f"claim: “{(item.evidence or {}).get('claim_quote') or ''}”\n"
        f"evidence: “{(item.evidence or {}).get('evidence_quote') or (item.evidence or {}).get('quoted_evidence') or ''}”\n"
        f"fact_named: {(item.evidence or {}).get('fact_named') or ''}\n"
        f"analysis: {(item.evidence or {}).get('analysis_type') or ''}\n"
        f"tainted: {(item.evidence or {}).get('tainted_support')}\n"
        f"explanation: {item.explanation[:400]}"
        for i, (_, item) in enumerate(items)
    )
    prompt = JUDGE_PROMPT.format(findings=blob)
    model = settings.claude_model or "claude-sonnet-4-5-20250929"
    payload = {
        "model": model,
        "max_tokens": 2048,
        "temperature": 0,
        "messages": [{"role": "user", "content": prompt}],
    }
    req = urllib.request.Request(
        "https://api.anthropic.com/v1/messages",
        data=json.dumps(payload).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "x-api-key": key,
            "anthropic-version": "2023-06-01",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=max(settings.gemini_timeout_s, 60)) as resp:
            body = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, json.JSONDecodeError):
        return {
            idx: {
                "impact": IMPACT_NOISE,
                "bucket": None,
                "rationale": "Judge unavailable — suppress ambiguous pair.",
                "gate": "judge_error",
            }
            for idx, _ in items
        }
    text = _anthropic_text(body)
    parsed = _extract_json(text)
    if not parsed:
        return {
            idx: {
                "impact": IMPACT_NOISE,
                "bucket": None,
                "rationale": "Judge parse failed — suppress.",
                "gate": "judge_parse",
            }
            for idx, _ in items
        }
    out: dict[int, dict[str, Any]] = {}
    for row in parsed.get("results") or []:
        if not isinstance(row, dict):
            continue
        try:
            local_i = int(row.get("index"))
        except (TypeError, ValueError):
            continue
        if local_i < 0 or local_i >= len(items):
            continue
        finding_idx = items[local_i][0]
        impact = str(row.get("impact") or IMPACT_NOISE).strip().lower()
        if impact not in {IMPACT_CASE_WINNING, IMPACT_MATERIAL, IMPACT_NOISE}:
            impact = IMPACT_NOISE
        bucket = row.get("bucket")
        if bucket in {"null", "None", ""}:
            bucket = None
        out[finding_idx] = {
            "impact": impact,
            "bucket": bucket,
            "rationale": str(row.get("rationale") or "")[:240],
            "gate": "claude",
        }
    # Missing indices → noise (uncertain)
    for idx, _ in items:
        if idx not in out:
            out[idx] = {
                "impact": IMPACT_NOISE,
                "bucket": None,
                "rationale": "No judge verdict — suppress.",
                "gate": "missing",
            }
    return out


def _apply_verdict(item: Finding, verdict: dict[str, Any]) -> Finding:
    impact = verdict.get("impact") or IMPACT_NOISE
    ev = dict(item.evidence or {})
    ev["case_impact"] = impact
    ev["case_impact_bucket"] = verdict.get("bucket")
    ev["case_impact_rationale"] = verdict.get("rationale") or ""
    ev["case_impact_gate"] = verdict.get("gate")
    updates: dict[str, Any] = {"evidence": ev}
    if impact == IMPACT_NOISE:
        updates["severity"] = "info"
        ev["material"] = False
    else:
        ev["material"] = True
        if impact == IMPACT_CASE_WINNING and item.severity in {"medium", "low", "info"}:
            updates["severity"] = "high"
    return item.model_copy(update=updates)


def _default_impact(item: Finding) -> str:
    if item.severity == "info":
        return IMPACT_NOISE
    if is_material_finding(item):
        return IMPACT_MATERIAL
    return IMPACT_NOISE


def _summary(findings: list[Finding]) -> dict[str, Any]:
    s = case_impact_summary(findings)
    return {
        "material_count": s["material_count"],
        "noise_count": s["noise_count"],
        "fraud_count": s["fraud_count"],
        "judged": True,
    }


def _anthropic_text(body: dict[str, Any]) -> str:
    parts = []
    for block in body.get("content") or []:
        if isinstance(block, dict) and block.get("type") == "text":
            parts.append(block.get("text") or "")
    return "\n".join(parts).strip()


def _extract_json(text: str) -> dict[str, Any] | None:
    if not text:
        return None
    try:
        data = json.loads(text)
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end <= start:
        return None
    try:
        data = json.loads(text[start : end + 1])
        return data if isinstance(data, dict) else None
    except json.JSONDecodeError:
        return None
