"""Compare a newly scanned document against prior case snapshots.

Emits findings on the *new* document that name the conflicting prior file and fact.

Court filings share counsel letterhead (e.g. "Partners, PLLC") and many different
signature / service dates in the same month. Those must not fan out into hundreds of
false "inconsistent with Doc X" alerts. Comparisons require a substantive shared party
or issuer — not counsel alone.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.engines import finding
from app.models import Finding
from app.scan_profile import COURT_FILING_KINDS


AMOUNT_RE = re.compile(
    r"(?i)(?:total|balance|amount\s+due|new\s+charges|payment)\D{0,24}"
    r"(\$?\s?\d{1,3}(?:,\d{3})*(?:\.\d{2})?)"
)

COUNSEL_ORG_RE = re.compile(
    r"(?i)\b("
    r"pllc|l\.l\.p\.?|llp|"
    r"attorney|attorneys|counsel|esquire|esq|"
    r"law\s+office|law\s+offices|law\s+firm|law\s+group|"
    r"partners,?\s+pllc"
    r")\b"
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
    new_doc_kind: str | None = None,
) -> list[Finding]:
    """Return case-consistency findings for the newly scanned document."""
    findings: list[Finding] = []
    new_orgs = _substance_orgs(new_entities.get("organizations") or [])
    new_locs = {_norm(x) for x in (new_entities.get("locations") or []) if x}
    new_dates = set(new_entities.get("dates") or [])
    new_amount_values = {a["value"] for a in new_amounts if a.get("value")}
    new_is_filing = _is_court_filing_kind(new_doc_kind)

    for prior in prior_snapshots:
        prior_name = prior.get("display_name") or prior.get("original_filename") or "prior document"
        prior_id = prior.get("document_id")
        prior_entities = prior.get("entities") or {}
        prior_orgs = _substance_orgs(prior_entities.get("organizations") or [])
        prior_locs = {_norm(x) for x in (prior_entities.get("locations") or []) if x}
        prior_dates = set(prior_entities.get("dates") or [])
        prior_amounts = {a.get("value") for a in (prior.get("amounts") or []) if a.get("value")}
        prior_is_filing = _is_court_filing_kind(prior.get("doc_kind"))

        shared_orgs = sorted(new_orgs & prior_orgs)
        same_fingerprint = bool(
            new_fingerprint and prior.get("text_fingerprint") and new_fingerprint == prior["text_fingerprint"]
        )

        if not shared_orgs and not same_fingerprint:
            continue

        # Near-duplicate under a different name.
        if same_fingerprint:
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

        # Two pleadings that only share counsel letterhead are not comparable for dates/locales.
        if new_is_filing and prior_is_filing and not shared_orgs:
            continue
        if not shared_orgs:
            continue

        # Pleadings reciting older exhibit dates/addresses is normal. Only compare
        # exhibit-to-exhibit for location/date/amount conflicts.
        filing_involved = new_is_filing or prior_is_filing

        # Same issuer / party, conflicting service locations (exhibit-style signal).
        if not filing_involved and new_locs and prior_locs:
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

        if not filing_involved and new_amount_values and prior_amounts:
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

        # Date conflicts: only for exhibit-style pairs with a substantive shared issuer.
        # Different signature dates across affirmations/motions in the same month are normal.
        if not filing_involved and new_dates and prior_dates:
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

    return findings[:8]


def _substance_orgs(names: list[str]) -> set[str]:
    """Organizations that are parties/issuers — not counsel letterhead."""
    out: set[str] = set()
    for name in names:
        if not name or _is_counsel_org(name):
            continue
        out.add(_norm(name))
    return out


def _is_counsel_org(name: str) -> bool:
    n = _norm(name)
    if not n:
        return True
    if n in {"partners, pllc", "partners pllc", "partners"}:
        return True
    if COUNSEL_ORG_RE.search(n):
        return True
    # Bare professional suffixes with almost no identity.
    if re.fullmatch(r"(partners|associates|attorneys?)(,?\s*(pllc|llp|llc|pc))?", n):
        return True
    return False


def _is_court_filing_kind(kind: str | None) -> bool:
    return (kind or "").strip().lower() in COURT_FILING_KINDS


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
