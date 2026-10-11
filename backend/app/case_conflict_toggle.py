"""Read-time switch for case-wide fact-conflict findings (CASE_FACT_CONFLICTS).

When the setting is off (default), findings whose analysis type is a fact
conflict or standing-timeline roll-up are removed from the case board and from
per-document reports, and each report's risk is re-scored without them.
Stored data is never changed, so turning the setting back on restores
everything without a rescan.
"""

from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger("uvicorn.error")

# Analysis types / categories produced by cross-document and fact-consistency
# checks (case_consistency.py). Per-document forensic findings (metadata, PDF
# structure, image forensics, statement dates, template fabrication, math,
# Gemini document review, per-document gotchas) use other types and are kept.
CASE_CONFLICT_TYPES = frozenset({"fact_conflict", "standing_timeline", "case_consistency"})


def case_fact_conflicts_enabled() -> bool:
    from app.config import settings

    return bool(getattr(settings, "case_fact_conflicts", False))


def is_case_fact_conflict(item: Any) -> bool:
    if not isinstance(item, dict):
        return False
    evidence = item.get("evidence") if isinstance(item.get("evidence"), dict) else {}
    kinds = {
        str(item.get("finding_type") or ""),
        str(item.get("analysis_type") or ""),
        str(item.get("category") or ""),
        str(evidence.get("analysis_type") or ""),
    }
    if evidence.get("case_consistency"):
        kinds.add("case_consistency")
    return bool(kinds & CASE_CONFLICT_TYPES)


def filter_findings(items: list[Any] | None) -> list[Any]:
    """Drop case-wide conflict findings when the setting is off."""
    items = list(items or [])
    if case_fact_conflicts_enabled():
        return items
    return [item for item in items if not is_case_fact_conflict(item)]


def strip_report(report: Any) -> Any:
    """Return a report dict without case-wide conflicts, re-scored.

    Leaves the input untouched. Non-dict input and reports with nothing to
    remove are returned as-is.
    """
    if case_fact_conflicts_enabled() or not isinstance(report, dict):
        return report
    findings = report.get("findings") or []
    kept = [f for f in findings if not is_case_fact_conflict(f)]
    top = report.get("top_findings") or []
    kept_top = [f for f in top if not is_case_fact_conflict(f)]
    if len(kept) == len(findings) and len(kept_top) == len(top):
        return report
    out = dict(report)
    out["findings"] = kept
    out["top_findings"] = kept_top
    try:
        from app.models import Finding
        from app.report.scoring import (
            executive_summary,
            pick_top_findings,
            recommended_actions,
            score_risk,
        )

        models = [Finding.model_validate(f) for f in kept if isinstance(f, dict)]
        level, score = score_risk(models)
        out["risk_level"] = level
        out["risk_score"] = score
        out["top_findings"] = [f.model_dump(mode="json") for f in pick_top_findings(models)]
        audit = out.get("audit") if isinstance(out.get("audit"), dict) else {}
        name = str(audit.get("filename") or "this document")
        out["executive_summary"] = executive_summary(level, name, models)
        out["recommended_actions"] = recommended_actions(level, models)
    except Exception:
        # Never fail a read over re-scoring; fall back to the filtered lists only.
        logger.warning("case-conflict re-score failed; serving filtered findings only")
    out["case_fact_conflicts_hidden"] = len(findings) - len(kept)
    return out
