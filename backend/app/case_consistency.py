"""Compare a newly scanned document against prior case snapshots.

Emits findings on the *new* document that name the conflicting prior file and fact.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.engines import finding
from app.models import Finding


AMOUNT_RE = re.compile(
    r"(?i)(?:total|balance|amount\s+due|new\s+charges|payment)\D{0,24}"
    r"(\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
)


def extract_amounts(text: str, limit: int = 12) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for match in AMOUNT_RE.finditer(text or ""):
        raw = re.sub(r"[^\d.]", "", match.group(1))
        if not raw or raw in seen:
            continue
        seen.add(raw)
        label = match.group(0)[:80]
        out.append({"label": label, "value": raw})
        if len(out) >= limit:
            break
    return out


def text_fingerprint(text: str) -> str:
    normalized = re.sub(r"\s+", " ", (text or "").lower()).strip()
    return hashlib.sha1(normalized[:8000].encode("utf-8", errors="ignore")).hexdigest()


def excerpt_hashes(text: str, size: int = 180, stride: int = 400) -> list[str]:
    body = re.sub(r"\s+", " ", (text or "")).strip()
    hashes: list[str] = []
    for start in range(0, min(len(body), 12_000), stride):
        chunk = body[start : start + size]
        if len(chunk) < 40:
            continue
        hashes.append(hashlib.sha1(chunk.encode("utf-8", errors="ignore")).hexdigest()[:16])
    return hashes[:30]


def findings_summary(findings: list[Finding]) -> list[dict[str, Any]]:
    summary: list[dict[str, Any]] = []
    for item in findings[:40]:
        summary.append(
            {
                "id": item.id,
                "category": item.category,
                "severity": item.severity,
                "title": item.title,
                "confidence": item.confidence,
                "analysis_type": (item.evidence or {}).get("analysis_type"),
            }
        )
    return summary


def compare_to_case(
    *,
    new_document_id: str,
    new_display_name: str,
    new_entities: dict[str, Any],
    new_amounts: list[dict[str, Any]],
    new_fingerprint: str | None,
    prior_snapshots: list[dict[str, Any]],
) -> list[Finding]:
    """Return case-consistency findings for the newly scanned document."""
    findings: list[Finding] = []
    new_orgs = {_norm(x) for x in (new_entities.get("organizations") or []) if x}
    new_locs = {_norm(x) for x in (new_entities.get("locations") or []) if x}
    new_dates = set(new_entities.get("dates") or [])
    new_amount_values = {a["value"] for a in new_amounts if a.get("value")}

    for prior in prior_snapshots:
        prior_name = prior.get("display_name") or prior.get("original_filename") or "prior document"
        prior_id = prior.get("document_id")
        prior_entities = prior.get("entities") or {}
        prior_orgs = {_norm(x) for x in (prior_entities.get("organizations") or []) if x}
        prior_locs = {_norm(x) for x in (prior_entities.get("locations") or []) if x}
        prior_dates = set(prior_entities.get("dates") or [])
        prior_amounts = {a.get("value") for a in (prior.get("amounts") or []) if a.get("value")}

        shared_orgs = sorted(new_orgs & prior_orgs)
        if not shared_orgs and not (new_fingerprint and new_fingerprint == prior.get("text_fingerprint")):
            # Unrelated documents in the same case are normal; only compare when they share an issuer
            # or look like near-duplicates.
            continue

        # Near-duplicate under a different name still deserves a note, not a fraud alert.
        if new_fingerprint and prior.get("text_fingerprint") and new_fingerprint == prior["text_fingerprint"]:
            findings.append(
                _case_finding(
                    prior_id=prior_id,
                    prior_name=prior_name,
                    field="content_fingerprint",
                    new_value=new_display_name,
                    prior_value=prior_name,
                    title=f"Possible duplicate of “{prior_name}”",
                    explanation=(
                        f"“{new_display_name}” appears to contain the same text fingerprint as "
                        f"“{prior_name}” already in this case. Confirm whether this is a re-file, "
                        "a stamped copy, or an unintended duplicate upload."
                    ),
                    why="Re-uploads and e-filing re-stamps are common; silent duplicates can also hide a swap.",
                    confidence=62,
                    severity="info",
                )
            )

        # Same issuer, conflicting service locations.
        if shared_orgs and new_locs and prior_locs:
            only_new = sorted(new_locs - prior_locs)
            only_prior = sorted(prior_locs - new_locs)
            if only_new and only_prior and not (new_locs & prior_locs):
                org = shared_orgs[0]
                findings.append(
                    _case_finding(
                        prior_id=prior_id,
                        prior_name=prior_name,
                        field="service_location",
                        new_value=", ".join(only_new[:3]),
                        prior_value=", ".join(only_prior[:3]),
                        title=f"Inconsistent with “{prior_name}” — service location",
                        explanation=(
                            f"Both documents mention {org}, but this file’s locations "
                            f"({', '.join(only_new[:3])}) do not overlap with locations in "
                            f"“{prior_name}” ({', '.join(only_prior[:3])})."
                        ),
                        why="The same account/issuer with disjoint service addresses can signal a swapped page.",
                        confidence=68,
                    )
                )

        # Shared org + shared label-ish amounts that disagree for the same numeric field family.
        if shared_orgs and new_amount_values and prior_amounts:
            # Flag only when both discuss totals/balances and values diverge sharply for same org pair.
            if not (new_amount_values & prior_amounts) and len(new_amount_values) <= 6 and len(prior_amounts) <= 6:
                findings.append(
                    _case_finding(
                        prior_id=prior_id,
                        prior_name=prior_name,
                        field="amounts",
                        new_value=", ".join(sorted(new_amount_values)[:4]),
                        prior_value=", ".join(sorted(x for x in prior_amounts if x)[:4]),
                        title=f"Inconsistent with “{prior_name}” — amounts",
                        explanation=(
                            f"Documents that both reference {shared_orgs[0]} list non-overlapping "
                            f"key amounts. This file: {', '.join(sorted(new_amount_values)[:4])}. "
                            f"“{prior_name}”: {', '.join(sorted(x for x in prior_amounts if x)[:4])}."
                        ),
                        why="Altered balances on a later-produced statement often disagree with an earlier exhibit.",
                        confidence=58,
                        severity="low",
                    )
                )

        # Date clusters for the same issuer that are years apart beyond packaging stamps.
        if shared_orgs and new_dates and prior_dates:
            new_years = {_year(d) for d in new_dates if _year(d)}
            prior_years = {_year(d) for d in prior_dates if _year(d)}
            if new_years and prior_years:
                gap = abs(min(new_years) - max(prior_years)) if min(new_years) > max(prior_years) else abs(
                    min(prior_years) - max(new_years)
                )
                # Large year gaps for the same issuer can be legitimate (multi-year case). Soft note only
                # when both claim the same narrow month window with conflicting days — skip broad gaps.
                overlap_months = _month_keys(new_dates) & _month_keys(prior_dates)
                if overlap_months and new_dates != prior_dates:
                    conflicting_new = sorted(d for d in new_dates if d[:7] in overlap_months)[:4]
                    conflicting_prior = sorted(d for d in prior_dates if d[:7] in overlap_months)[:4]
                    if set(conflicting_new) != set(conflicting_prior):
                        findings.append(
                            _case_finding(
                                prior_id=prior_id,
                                prior_name=prior_name,
                                field="dates",
                                new_value=", ".join(conflicting_new),
                                prior_value=", ".join(conflicting_prior),
                                title=f"Inconsistent with “{prior_name}” — dates",
                                explanation=(
                                    f"Both files mention {shared_orgs[0]} with dates in the same month(s) "
                                    f"({', '.join(sorted(overlap_months)[:3])}) that do not match. "
                                    f"This file: {', '.join(conflicting_new)}. "
                                    f"“{prior_name}”: {', '.join(conflicting_prior)}."
                                ),
                                why="Edited statement dates often leave a twin exhibit with the original calendar.",
                                confidence=70,
                            )
                        )

    return findings[:12]


def _case_finding(
    *,
    prior_id: str | None,
    prior_name: str,
    field: str,
    new_value: str,
    prior_value: str,
    title: str,
    explanation: str,
    why: str,
    confidence: float,
    severity: str = "medium",
) -> Finding:
    seed = f"case|{prior_id}|{field}|{new_value}|{prior_value}"
    fid = "ctx-case-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return finding(
        fid=fid,
        category="case_consistency",
        severity=severity,  # type: ignore[arg-type]
        title=title,
        explanation=explanation,
        why_suspicious=why,
        confidence=confidence,
        evidence={
            "case_consistency": True,
            "prior_document_id": prior_id,
            "prior_display_name": prior_name,
            "field": field,
            "new_value": new_value,
            "prior_value": prior_value,
        },
        recommended_action=(
            f"Open “{prior_name}” side by side with this file and confirm which fact is reliable "
            "before relying on either exhibit."
        ),
    )


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _year(iso: str) -> int | None:
    try:
        return int(str(iso)[:4])
    except ValueError:
        return None


def _month_keys(dates: set[str]) -> set[str]:
    return {d[:7] for d in dates if len(d) >= 7}
