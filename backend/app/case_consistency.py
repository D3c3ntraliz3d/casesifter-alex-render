"""Compare a newly scanned document against prior case snapshots.

Only MATERIAL contradictions interrupt the attorney: the same labeled fact
(loan amount, rate, payment, balance, key date) that exceeds Claude's thresholds,
a referenced exhibit that is not in the case, or a near-duplicate upload.

Name abbreviations (WaMu / Washington Mutual) and same-month day shifts are
not flags. Court filings are compared on labeled facts — that is how a motion
disagrees with a statement of material facts.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.engines import finding
from app.fact_extract import FACT_META, GOTCHA_KEYS, extract_facts, referenced_exhibits
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

EXHIBIT_NAME_RE = re.compile(r"(?i)\bexhibit\s+([A-Z0-9]{1,3})\b")


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
                "gotcha": bool((item.evidence or {}).get("gotcha")),
                "gotcha_level": (item.evidence or {}).get("gotcha_level"),
                "contradiction_type": (item.evidence or {}).get("contradiction_type"),
                "verified": bool((item.evidence or {}).get("verified")),
                "claim_quote": (item.evidence or {}).get("claim_quote"),
                "evidence_quote": (item.evidence or {}).get("evidence_quote"),
                "claim_source": (item.evidence or {}).get("claim_source"),
                "evidence_source": (item.evidence or {}).get("evidence_source"),
                "fact_named": (item.evidence or {}).get("fact_named"),
                "direction_reason": (item.evidence or {}).get("direction_reason"),
                "materiality_reason": (item.evidence or {}).get("materiality_reason"),
                "attorney_why": (item.evidence or {}).get("attorney_why"),
                "evidence_label": (item.evidence or {}).get("evidence_label"),
                "evidence_tier": (item.evidence or {}).get("evidence_tier"),
                "self_undercut": (item.evidence or {}).get("self_undercut"),
                "document_id": (item.evidence or {}).get("prior_document_id")
                or item.evidence.get("document_id")
                if isinstance(item.evidence, dict)
                else None,
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
    new_facts: list[dict[str, Any]] | None = None,
    new_text: str | None = None,
    case_documents: list[dict[str, Any]] | None = None,
) -> tuple[list[Finding], list[dict[str, Any]]]:
    """Return (findings, fact matrix rows) for the newly scanned document."""
    del new_amounts  # unlabeled dollar dumps are too noisy; labeled facts only
    findings: list[Finding] = []
    matrix: list[dict[str, Any]] = []
    facts = list(new_facts or [])
    if not facts and new_text:
        facts = extract_facts(new_text)
    findings.extend(_internal_fact_conflicts(new_display_name, facts))

    known_exhibits = _known_exhibits(case_documents, prior_snapshots, new_display_name)
    for tag in referenced_exhibits(new_text or ""):
        if tag in known_exhibits:
            continue
        findings.append(
            _case_finding(
                prior_id=None,
                prior_name="case file",
                field="missing_exhibit",
                new_value=f"Exhibit {tag}",
                prior_value="not in case",
                title=f"Referenced Exhibit {tag} is not in this case",
                explanation=(
                    f"“{new_display_name}” refers to Exhibit {tag} as attached or annexed, "
                    "but no document in this case is titled that exhibit."
                ),
                why="A motion that relies on a missing exhibit cannot be verified against the record you have.",
                confidence=78,
                severity="high",
                material=True,
                recommended_action=f"Upload Exhibit {tag} or confirm the reference is a docket citation only.",
            )
        )

    new_orgs = _substance_orgs(new_entities.get("organizations") or [])
    new_locs = {_norm(x) for x in (new_entities.get("locations") or []) if x}
    new_is_filing = _is_court_filing_kind(new_doc_kind)

    for prior in prior_snapshots:
        prior_name = prior.get("display_name") or prior.get("original_filename") or "prior document"
        prior_id = prior.get("document_id")
        prior_entities = prior.get("entities") or {}
        prior_orgs = _substance_orgs(prior_entities.get("organizations") or [])
        prior_locs = {_norm(x) for x in (prior_entities.get("locations") or []) if x}
        prior_is_filing = _is_court_filing_kind(prior.get("doc_kind"))
        prior_facts = list(prior.get("facts") or [])
        if not prior_facts:
            prior_facts = extract_facts(prior.get("text_excerpt") or "")

        shared_orgs = sorted(new_orgs & prior_orgs)
        same_fingerprint = bool(
            new_fingerprint and prior.get("text_fingerprint") and new_fingerprint == prior["text_fingerprint"]
        )

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
                    material=False,
                )
            )

        for row in _compare_facts(facts, prior_facts, prior_name):
            matrix.append({"prior_document_id": prior_id, "prior_name": prior_name, **row})
            # Cross-document fact mismatches are one case-level alert, not a
            # per-file flag on every document that mentions the same number.

        # Exhibit-to-exhibit location split still matters; pleadings share many addresses.
        filing_involved = new_is_filing or prior_is_filing
        if not filing_involved and shared_orgs and new_locs and prior_locs:
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
                        severity="info",
                        material=False,
                    )
                )

    return findings[:12], matrix[:40]


def _internal_fact_conflicts(doc_name: str, facts: list[dict[str, Any]]) -> list[Finding]:
    grouped: dict[str, list[dict[str, Any]]] = {}
    for fact in facts:
        grouped.setdefault(fact["key"], []).append(fact)
    out: list[Finding] = []
    for key, items in grouped.items():
        if key not in GOTCHA_KEYS:
            continue
        unique = _unique_facts(items)
        if len(unique) < 2:
            continue
        row = _score_pair(unique[0], unique[1], key)
        if not row or not row.get("flag"):
            continue
        out.append(
            _case_finding(
                prior_id=None,
                prior_name=doc_name,
                field=key,
                new_value=row["this_display"],
                prior_value=row["prior_display"],
                title=f"{row['severity_label']}: {row['label']} conflicts inside this document",
                explanation=(
                    f"This file states both “{row['this_quote']}” and “{row['prior_quote']}”. "
                    f"Discrepancy: {row['discrepancy']}. {row['materiality']}"
                ),
                why=row["materiality"],
                confidence=row["confidence"],
                severity=row["severity"],
                material=row["flag"] == "red",
                recommended_action="Reconcile the two figures in this filing before using either number.",
            )
        )
    return out


def _compare_facts(
    new_facts: list[dict[str, Any]],
    prior_facts: list[dict[str, Any]],
    prior_name: str,
) -> list[dict[str, Any]]:
    del prior_name
    rows: list[dict[str, Any]] = []
    prior_by_key: dict[str, list[dict[str, Any]]] = {}
    for fact in prior_facts:
        prior_by_key.setdefault(fact["key"], []).append(fact)
    seen: set[str] = set()
    for fact in new_facts:
        key = fact["key"]
        if key not in GOTCHA_KEYS or key in seen:
            continue
        counterparts = prior_by_key.get(key) or []
        if not counterparts:
            continue
        seen.add(key)
        best = None
        for other in _unique_facts(counterparts):
            row = _score_pair(fact, other, key)
            if row is None:
                continue
            if best is None or (row.get("flag") == "red" and best.get("flag") != "red"):
                best = row
            elif row.get("flag") and not best.get("flag"):
                best = row
            elif best is None:
                best = row
        if best:
            rows.append(best)
    return rows


def _score_pair(left: dict[str, Any], right: dict[str, Any], key: str) -> dict[str, Any] | None:
    meta = FACT_META.get(key) or {}
    kind = meta.get("kind") or left.get("kind")
    label = meta.get("label") or left.get("label") or key
    if kind == "money":
        a, b = float(left["value"]), float(right["value"])
        delta = abs(a - b)
        baseline = max(a, b, 1.0)
        pct = delta / baseline
        over = delta > meta.get("abs", 0) or pct > meta.get("pct", 0)
        borderline = (not over) and (delta > meta.get("abs", 0) * 0.6 or pct > meta.get("pct", 0) * 0.6)
        discrepancy = f"${delta:,.2f} ({pct:.1%})"
        materiality = (
            f"{label} flags when the difference exceeds ${meta.get('abs', 0):,.0f} or {meta.get('pct', 0):.1%}."
        )
        return _row(left, right, key, label, discrepancy, materiality, over, borderline, confidence=82 if over else 55)
    if kind == "rate":
        a, b = float(left["value"]), float(right["value"])
        delta = abs(a - b)
        over = delta > meta.get("abs", 0.1)
        borderline = (not over) and delta > 0.05
        discrepancy = f"{delta:.3f} percentage points"
        materiality = f"Interest rate flags when it differs by more than {meta.get('abs', 0.1)} points."
        return _row(left, right, key, label, discrepancy, materiality, over, borderline, confidence=88 if over else 58)
    if kind == "date":
        if left.get("value") == right.get("value"):
            return _row(left, right, key, label, "same date", "Dates match.", False, False, confidence=40)
        same_month = left.get("month_key") and left.get("month_key") == right.get("month_key")
        deadline = bool(meta.get("deadline"))
        if same_month and not deadline:
            return _row(
                left,
                right,
                key,
                label,
                "same month, different day",
                "Same month/year with a different day is not flagged unless the date is deadline-critical.",
                False,
                False,
                confidence=30,
            )
        over = (left.get("month_key") != right.get("month_key")) or deadline
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        materiality = (
            "Date flags when the month or year differs, or when a notice/foreclosure deadline day does not match."
        )
        return _row(left, right, key, label, discrepancy, materiality, over, False, confidence=76 if over else 40)
    return None


def _row(
    left: dict[str, Any],
    right: dict[str, Any],
    key: str,
    label: str,
    discrepancy: str,
    materiality: str,
    over: bool,
    borderline: bool,
    confidence: float,
) -> dict[str, Any]:
    if over:
        flag, severity, severity_label = "red", "high", "RED FLAG"
    elif borderline:
        flag, severity, severity_label = "yellow", "low", "YELLOW FLAG"
    else:
        flag, severity, severity_label = None, "info", "MATCH"
    return {
        "key": key,
        "label": label,
        "this_display": left.get("display"),
        "prior_display": right.get("display"),
        "this_quote": left.get("quote") or left.get("display"),
        "prior_quote": right.get("quote") or right.get("display"),
        "discrepancy": discrepancy,
        "materiality": materiality,
        "flag": flag,
        "severity": severity,
        "severity_label": severity_label,
        "confidence": confidence,
        "match": not over and not borderline,
    }


def _unique_facts(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    seen: set[str] = set()
    out: list[dict[str, Any]] = []
    for item in items:
        token = str(item.get("value"))
        if token in seen:
            continue
        seen.add(token)
        out.append(item)
    return out


def _known_exhibits(
    case_documents: list[dict[str, Any]] | None,
    priors: list[dict[str, Any]],
    new_name: str,
) -> set[str]:
    tags: set[str] = set()
    for source in list(case_documents or []) + list(priors) + [{"display_name": new_name}]:
        hay = f"{source.get('display_name') or ''} {source.get('original_filename') or ''}"
        for match in EXHIBIT_NAME_RE.finditer(hay):
            tags.add(match.group(1).upper())
    return tags


def _substance_orgs(names: list[str]) -> set[str]:
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
    material: bool = False,
    recommended_action: str | None = None,
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
            "material": material,
        },
        recommended_action=recommended_action
        or (
            f"Open “{prior_name}” side by side with this file and confirm which fact is reliable "
            "before relying on either exhibit."
        ),
    )


def _norm(value: str) -> str:
    return re.sub(r"\s+", " ", value or "").strip().lower()


def _quote_key(text: str) -> str:
    return re.sub(r"\s+", " ", (text or "").strip().lower())[:240]


def dedupe_gotchas(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One card per claim/evidence pair; later hits add related documents."""
    grouped: dict[tuple[str, str], dict[str, Any]] = {}
    order: list[tuple[str, str]] = []
    for item in items:
        claim = _quote_key(str(item.get("claim_quote") or item.get("title") or ""))
        evidence = _quote_key(str(item.get("evidence_quote") or ""))
        key = tuple(sorted((claim, evidence)))
        related = {
            "document_id": item.get("document_id"),
            "display_name": item.get("display_name") or item.get("claim_source") or item.get("evidence_source"),
        }
        if key not in grouped:
            grouped[key] = {
                **item,
                "related_documents": [related] if related.get("document_id") else [],
            }
            order.append(key)
            continue
        existing = grouped[key]
        seen = {doc.get("document_id") for doc in existing.get("related_documents") or []}
        if related.get("document_id") and related["document_id"] not in seen:
            existing.setdefault("related_documents", []).append(related)
    return [grouped[key] for key in order]


def aggregate_fact_conflicts(snapshots: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One conflict per labeled fact, listing every file and value."""
    by_key: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for snap in snapshots:
        for fact in snap.get("facts") or []:
            key = str(fact.get("key") or "")
            if key not in GOTCHA_KEYS:
                continue
            by_key.setdefault(key, []).append((snap, fact))

    out: list[dict[str, Any]] = []
    for key, pairs in by_key.items():
        variants: dict[str, dict[str, Any]] = {}
        for snap, fact in pairs:
            token = str(fact.get("value"))
            doc_id = snap.get("document_id")
            name = snap.get("display_name") or snap.get("original_filename") or "document"
            bucket = variants.setdefault(
                token,
                {
                    "value": token,
                    "display": fact.get("display") or token,
                    "quote": fact.get("quote") or fact.get("display"),
                    "fact": fact,
                    "documents": [],
                },
            )
            if not any(doc.get("document_id") == doc_id for doc in bucket["documents"]):
                bucket["documents"].append({"document_id": doc_id, "display_name": name})
        if len(variants) < 2:
            continue
        flagged = False
        worst = "low"
        confidence = 70
        unique_facts = [bucket["fact"] for bucket in variants.values()]
        for idx, left in enumerate(unique_facts):
            for right in unique_facts[idx + 1 :]:
                row = _score_pair(left, right, key)
                if row and row.get("flag"):
                    flagged = True
                    if row.get("severity") == "high":
                        worst = "high"
                        confidence = max(confidence, int(row.get("confidence") or 70))
        if not flagged:
            continue
        meta = FACT_META.get(key) or {}
        label = str(meta.get("label") or key)
        related = []
        seen_docs: set[str] = set()
        for bucket in variants.values():
            for doc in bucket["documents"]:
                doc_id = str(doc.get("document_id") or "")
                if doc_id and doc_id not in seen_docs:
                    seen_docs.add(doc_id)
                    related.append(doc)
        variant_bits = [
            f"{bucket['display']} in "
            + ", ".join(f"“{doc['display_name']}”" for doc in bucket["documents"][:6])
            for bucket in variants.values()
        ]
        out.append(
            {
                "id": "fact-" + hashlib.sha1(key.encode("utf-8")).hexdigest()[:10],
                "title": f"{label} does not match across the case",
                "gotcha_level": "Case fact conflict",
                "contradiction_type": "factual_contradiction",
                "verified": True,
                "gotcha": False,
                "analysis_type": "fact_conflict",
                "field": key,
                "severity": worst,
                "confidence": confidence,
                "attorney_why": (
                    f"{label} appears as more than one operative figure. "
                    + "; ".join(variant_bits)
                    + ". Confirm which figure controls before relying on any filing."
                ),
                "variants": [
                    {
                        "display": bucket["display"],
                        "quote": bucket["quote"],
                        "documents": bucket["documents"],
                    }
                    for bucket in variants.values()
                ],
                "related_documents": related,
                "document_id": related[0]["document_id"] if related else None,
                "display_name": related[0]["display_name"] if related else None,
            }
        )
    return out
