"""Compare a newly scanned document against prior case snapshots.

Only MATERIAL contradictions interrupt the attorney: a key date that cannot
coexist, same-CRFN discharge vs continued reliance, or a near-duplicate upload.
Exhibit letter/number labels routinely change between initial filing and the
recorded packet — citing “Exhibit A” when the case file uses another label is
not a flag. Note rate, default rate, principal balance, and installment amount
are expected to move with payment and default — they are not flags.

Name abbreviations (WaMu / Washington Mutual) and same-month day shifts are
not flags. Court filings are compared on labeled facts — that is how a motion
disagrees with a statement of material facts.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app.engines import finding
from app.fact_extract import FACT_META, GOTCHA_KEYS, extract_facts
from app.fact_graph import cluster_key, facts_compatible, same_nameable_fact
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

# NYC ACRIS recording cover sheets ("CROSS REFERENCE DATA") cite the county
# clerk's recording number as "CRFN: 2008000207462" — sometimes with no space
# at all between the label and the digits, or between the digits and the next
# word, when a scanned/re-rendered PDF's text layer drops whitespace at line
# breaks (seen throughout NYSCEF exhibits). Matching bare digits after "CRFN"
# handles both.
CRFN_RE = re.compile(r"(?i)\bCRFN\b[:\s]*?(\d{10,20})")

# Same whitespace-collapse problem shows up worst in scanned judgments: a real
# example read "...it is\nORDEREDANDADJUDGED\nthat the...be cancelled\nand"
# "discharged of record" with no spaces at several of those joins. Matching on
# a whitespace-stripped copy of the text sidesteps that unpredictability
# entirely instead of trying to enumerate which joins lose their space.
DISCHARGE_ADJUDICATION_RE = re.compile(
    r"orderedandadjudged.{0,1500}?cancell?edanddischargedofrecord"
)

# Distinguishes "the mortgage/complaint that names this recording number" from
# "an exhibit that happens to mention it": the Summons/Complaint is what
# establishes which recorded instrument this whole case is actually about.
COMPLAINT_NAME_RE = re.compile(r"(?i)\bcomplaint\b")


def _squash(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


def _crfns_in(text: str) -> set[str]:
    return {m.group(1) for m in CRFN_RE.finditer(text or "")}


def _document_discharges_mortgage(text: str) -> bool:
    return bool(DISCHARGE_ADJUDICATION_RE.search(_squash(text)))


def _looks_like_complaint(name: str) -> bool:
    return bool(COMPLAINT_NAME_RE.search(name or ""))


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
    gotchas = [item for item in findings if (item.evidence or {}).get("gotcha")]
    rest = [item for item in findings if item not in gotchas]
    for item in (gotchas + rest)[:40]:
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
                "tainted_support": (item.evidence or {}).get("tainted_support"),
                "case_impact": (item.evidence or {}).get("case_impact"),
                "case_impact_bucket": (item.evidence or {}).get("case_impact_bucket"),
                "case_impact_rationale": (item.evidence or {}).get("case_impact_rationale"),
                "explanation": item.explanation,
                "why_suspicious": item.why_suspicious,
                "quoted_evidence": (item.evidence or {}).get("quoted_evidence"),
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
    del case_documents  # exhibit-letter title matching is not an interrupt (labels change)
    findings: list[Finding] = []
    matrix: list[dict[str, Any]] = []
    facts = list(new_facts or [])
    if not facts and new_text:
        facts = extract_facts(new_text)

    # The recording number(s) the Complaint itself cites are what this case's
    # loan/monthly-payment/balance facts are actually "operative" for. An
    # exhibit that cites a *different* recording number (a bundled "Mortgages"
    # exhibit covering some other lien on the property, a different lender, a
    # different address entirely) legitimately restates its own different
    # dollar figures — that is not the same fact disagreeing with itself, it
    # is a different loan. Only exclude a document's money facts when it
    # positively cites a recording number and none of them match the
    # complaint's — silence on CRFN (the ordinary case for a plain affidavit
    # or a Note that never quotes its own recording number) changes nothing.
    complaint_crfns: set[str] = set()
    for prior in prior_snapshots:
        prior_name = prior.get("display_name") or prior.get("original_filename") or ""
        if _looks_like_complaint(prior_name):
            complaint_crfns |= _crfns_in(prior.get("text_excerpt") or "")
    if _looks_like_complaint(new_display_name):
        complaint_crfns |= _crfns_in(new_text or "")

    if complaint_crfns:
        this_crfns = _crfns_in(new_text or "")
        if this_crfns and not (this_crfns & complaint_crfns):
            facts = [f for f in facts if f.get("kind") != "money"]

    findings.extend(_internal_fact_conflicts(new_display_name, facts))
    findings.extend(
        _discharged_mortgage_findings(
            new_text=new_text or "",
            new_display_name=new_display_name,
            prior_snapshots=prior_snapshots,
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
        if complaint_crfns:
            prior_own_crfns = _crfns_in(prior.get("text_excerpt") or "")
            if prior_own_crfns and not (prior_own_crfns & complaint_crfns):
                prior_facts = [f for f in prior_facts if f.get("kind") != "money"]

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
            # Documentary graph conflicts (entity-scoped) interrupt as credibility —
            # party's own exhibits cannot both be true on the same nameable fact.
            if row.get("flag") == "red" and row.get("documentary"):
                findings.append(
                    _case_finding(
                        prior_id=prior_id,
                        prior_name=prior_name,
                        field=str(row.get("key") or "fact"),
                        new_value=str(row.get("this_display") or ""),
                        prior_value=str(row.get("prior_display") or ""),
                        title=(
                            f"{row.get('severity_label')}: {row.get('label')} "
                            f"does not match “{prior_name}”"
                        ),
                        explanation=(
                            f"This file states “{row.get('this_quote')}” while "
                            f"“{prior_name}” states “{row.get('prior_quote')}”. "
                            f"Discrepancy: {row.get('discrepancy')}. {row.get('materiality')}"
                        ),
                        why=(
                            "Same nameable fact with incompatible documentary values "
                            "is a credibility interrupt — the packet's own exhibits undercut each other."
                        ),
                        confidence=float(row.get("confidence") or 78),
                        severity=str(row.get("severity") or "high"),
                        material=True,
                        recommended_action=(
                            f"Reconcile “{new_display_name}” with “{prior_name}” before "
                            "relying on either exhibit."
                        ),
                    )
                )
                # Stamp credibility / documentary gates onto the finding evidence.
                if findings and isinstance(findings[-1].evidence, dict):
                    findings[-1].evidence.update(
                        {
                            "documentary": True,
                            "analysis_type": "fact_conflict",
                            "contradiction_type": "factual_contradiction",
                            "case_impact": True,
                            "case_impact_bucket": row.get("case_impact_bucket") or "credibility",
                            "case_impact_rationale": (
                                "Party's own documentary exhibits state incompatible values "
                                "for the same nameable fact."
                            ),
                            "claim_quote": row.get("this_quote"),
                            "evidence_quote": row.get("prior_quote"),
                            "fact_named": row.get("label"),
                        }
                    )
            # Other cross-document fact mismatches stay case-level via aggregate_fact_conflicts.

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


def _discharged_mortgage_findings(
    *,
    new_text: str,
    new_display_name: str,
    prior_snapshots: list[dict[str, Any]],
) -> list[Finding]:
    """Catch a mortgage foreclosure on a lien already discharged of record.

    Confirmed against a real case: a "Mortgages" exhibit buried a certified
    2016 judgment ordering the *exact* recording number the Complaint sues
    on ("CRFN 2008000207462", $533,850.00) "cancelled and discharged of
    record" years before this foreclosure was filed — the single most
    material fact in the case — and none of the three analysis engines
    flagged it; the exhibit scored "Low risk". This only flags when the same
    CRFN shows up in one document with an explicit court-ordered discharge
    and in another with no such language. It is not full chain-of-title
    reasoning — confirm the actual recorded history before relying on it.
    """
    new_crfns = _crfns_in(new_text)
    if not new_crfns:
        return []
    new_discharges = _document_discharges_mortgage(new_text)
    out: list[Finding] = []
    for prior in prior_snapshots:
        prior_text = prior.get("text_excerpt") or ""
        if not prior_text:
            continue
        prior_crfns = _crfns_in(prior_text)
        shared = new_crfns & prior_crfns
        if not shared:
            continue
        prior_discharges = _document_discharges_mortgage(prior_text)
        if new_discharges == prior_discharges:
            continue  # need exactly one side asserting the discharge
        prior_name = prior.get("display_name") or prior.get("original_filename") or "prior document"
        prior_id = prior.get("document_id")
        discharging_name = new_display_name if new_discharges else prior_name
        referencing_name = prior_name if new_discharges else new_display_name
        for crfn in sorted(shared):
            out.append(
                _case_finding(
                    prior_id=prior_id,
                    prior_name=prior_name,
                    field="mortgage_discharge",
                    new_value=f"CRFN {crfn} discharged — {discharging_name}",
                    prior_value=f"CRFN {crfn} relied on — {referencing_name}",
                    title=f"Mortgage CRFN {crfn} may already be discharged of record",
                    explanation=(
                        f"“{discharging_name}” contains a court order adjudging the mortgage "
                        f"recorded under CRFN {crfn} cancelled and discharged of record, while "
                        f"“{referencing_name}” cites that same CRFN as if the lien is still in "
                        "force. Confirm which is accurate before relying on either filing — "
                        "foreclosing a mortgage that was already discharged of record is a "
                        "complete defense."
                    ),
                    why=(
                        "A prior court order discharging the exact recording number being "
                        "foreclosed is one of the most material facts a case can contain."
                    ),
                    confidence=74,
                    severity="high",
                    material=True,
                    recommended_action=(
                        f"Pull the recorded chain of title for CRFN {crfn} and confirm whether "
                        "the mortgage being foreclosed here was actually discharged."
                    ),
                )
            )
    return out


def _internal_fact_conflicts(doc_name: str, facts: list[dict[str, Any]]) -> list[Finding]:
    """Flag incompatible values for the same nameable fact inside one binder/file."""
    candidates = [
        fact
        for fact in facts
        if fact.get("key") in GOTCHA_KEYS or fact.get("graph")
    ]
    out: list[Finding] = []
    seen_pairs: set[tuple[str, str]] = set()
    for i, left in enumerate(candidates):
        for right in candidates[i + 1 :]:
            if not same_nameable_fact(left, right):
                continue
            if facts_compatible(left, right):
                continue
            pair_token = tuple(
                sorted([f"{left.get('key')}:{left.get('value')}", f"{right.get('key')}:{right.get('value')}"])
            )
            if pair_token in seen_pairs:
                continue
            seen_pairs.add(pair_token)
            row = _score_graph_or_meta(left, right)
            if not row or not row.get("flag"):
                continue
            evidence_extra = {
                "case_consistency": True,
                "analysis_type": "fact_conflict",
                "contradiction_type": "factual_contradiction",
                "documentary": True,
                "graph": bool(left.get("graph") or right.get("graph")),
                "case_impact": True,
                "case_impact_bucket": row.get("case_impact_bucket") or "credibility",
                "case_impact_rationale": (
                    "Party's own documentary exhibits state incompatible values "
                    "for the same nameable fact."
                ),
                "claim_quote": row["this_quote"],
                "evidence_quote": row["prior_quote"],
                "fact_named": row.get("label"),
                "attorney_why": row.get("materiality"),
            }
            finding_obj = _case_finding(
                prior_id=None,
                prior_name=doc_name,
                field=str(row.get("key") or left.get("key")),
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
            if isinstance(finding_obj.evidence, dict):
                finding_obj.evidence.update(evidence_extra)
            out.append(finding_obj)
    return out


def _score_graph_or_meta(left: dict[str, Any], right: dict[str, Any]) -> dict[str, Any] | None:
    """Score a conflict; handles same-key meta pairs and cross-attribute graph rules."""
    keys = {left.get("key"), right.get("key")}
    if keys == {"employment_start", "employment_tax_year"}:
        start = left if left.get("key") == "employment_start" else right
        tax = right if left.get("key") == "employment_start" else left
        label = "Employment tenure vs W-2 year"
        discrepancy = f"hire/start {start.get('display')} vs W-2 year {tax.get('display')}"
        materiality = (
            "A W-2 for a tax year asserts wages that year; a later hire/start date "
            "for the same person and employer cannot both be true."
        )
        row = _row(
            start,
            tax,
            "employment_start",
            label,
            discrepancy,
            materiality,
            True,
            False,
            confidence=86,
        )
        row["documentary"] = True
        return row
    if keys == {"occupancy_start", "mortgage_origination"}:
        occ = left if left.get("key") == "occupancy_start" else right
        mort = right if left.get("key") == "occupancy_start" else left
        label = "Occupancy start vs mortgage origination"
        discrepancy = f"occupancy {occ.get('display')} vs mortgage {mort.get('display')}"
        materiality = (
            "Claimed occupancy beginning before mortgage origination is temporally "
            "impossible — credibility / chronology interrupt on the party's own papers."
        )
        row = _row(occ, mort, "occupancy_start", label, discrepancy, materiality, True, False, confidence=88)
        row["documentary"] = True
        row["case_impact_bucket"] = "credibility"
        return row
    key = str(left.get("key") or right.get("key") or "")
    row = _score_pair(left, right, key)
    if row:
        row["documentary"] = bool(left.get("documentary") or right.get("documentary") or left.get("graph"))
    return row


def _compare_facts(
    new_facts: list[dict[str, Any]],
    prior_facts: list[dict[str, Any]],
    prior_name: str,
) -> list[dict[str, Any]]:
    del prior_name
    rows: list[dict[str, Any]] = []
    prior_candidates = [
        fact
        for fact in prior_facts
        if fact.get("key") in GOTCHA_KEYS or fact.get("graph")
    ]
    seen: set[str] = set()
    for fact in new_facts:
        key = fact.get("key")
        if key not in GOTCHA_KEYS and not fact.get("graph"):
            continue
        ck = cluster_key(fact)
        if ck in seen:
            continue
        counterparts = [other for other in prior_candidates if same_nameable_fact(fact, other)]
        if not counterparts:
            continue
        seen.add(ck)
        best = None
        for other in _unique_facts(counterparts):
            if facts_compatible(fact, other):
                # Still record soft match rows for same-key dates that match
                if fact.get("key") == other.get("key"):
                    row = _score_graph_or_meta(fact, other)
                    if row and best is None:
                        best = row
                continue
            row = _score_graph_or_meta(fact, other)
            if row is None:
                continue
            if best is None or (row.get("flag") == "red" and best.get("flag") != "red"):
                best = row
            elif row.get("flag") and not best.get("flag"):
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
        if key == "annual_income":
            materiality = (
                "Stated income and tax AGI for the same person cannot both be operative when "
                "they diverge this far — credibility interrupt on the financial story."
            )
        if key == "stated_principal":
            roles = {left.get("principal_role"), right.get("principal_role")}
            if roles == {"note", "judgment"} and float(left["value"]) != float(right["value"]):
                materiality = (
                    "Judgment principal that is materially lower than the note/assignment "
                    "principal while the packet asserts arrears / missed payments (no payment "
                    "history) is a damages interrupt — not ordinary loan-status movement."
                )
                over = True
                borderline = False
                discrepancy = f"${delta:,.2f} principal decline ({pct:.1%})"
        row = _row(left, right, key, label, discrepancy, materiality, over, borderline, confidence=82 if over else 55)
        if key == "annual_income":
            row["documentary"] = True
        if key == "stated_principal" and over:
            row["documentary"] = True
            row["case_impact_bucket"] = "damages"
        return row
    if kind == "presence":
        from app.fact_graph import facts_compatible as _fc

        if _fc(left, right):
            return _row(
                left,
                right,
                key,
                label,
                "compatible presence",
                "Presence facts for this person/date are compatible.",
                False,
                False,
                confidence=40,
            )
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        statuses = {left.get("presence_status"), right.get("presence_status")}
        materiality = (
            "Same person cannot be personally served at a local address and be "
            "out-of-state / in flight / at a remote conference on the same date — "
            "credibility / service element interrupt."
        )
        bucket = "element" if "served_personally" in statuses else "credibility"
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=88)
        row["documentary"] = True
        row["case_impact_bucket"] = bucket
        return row
    if kind == "service_attempt":
        from app.fact_graph import facts_compatible as _fc

        if _fc(left, right):
            return _row(
                left,
                right,
                key,
                label,
                "compatible service attempts",
                "Service-attempt facts are compatible.",
                False,
                False,
                confidence=40,
            )
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        materiality = (
            "Process server's sworn personal-service AOS collides with the server's own "
            "daily log (nail-and-mail only and/or different address near the claimed time) — "
            "credibility / sewerservice risk on plaintiff's own papers."
        )
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=90)
        row["documentary"] = True
        row["case_impact_bucket"] = "credibility"
        return row
    if kind == "money_on_date":
        from app.fact_graph import facts_compatible as _fc

        if _fc(left, right):
            return _row(left, right, key, label, "same balance", "Balances match.", False, False, confidence=40)
        try:
            a, b = float(left["value"]), float(right["value"])
            delta = abs(a - b)
            pct = delta / max(a, b, 1.0)
            discrepancy = f"${delta:,.2f} ({pct:.1%}) on {left.get('on_date') or right.get('on_date')}"
        except (TypeError, ValueError, KeyError):
            discrepancy = f"{left.get('display')} vs {right.get('display')}"
        if key == "claimed_arrears":
            materiality = (
                "Claimed arrears that drop sharply across named complaint dates without "
                "documented payments is a damages / financial impossibility interrupt — "
                "not ordinary loan-status movement."
            )
        else:
            materiality = (
                "Two incompatible balances for the same named date cannot both be operative — "
                "damages / financial impossibility interrupt."
            )
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=86)
        row["documentary"] = True
        row["case_impact_bucket"] = "damages"
        return row
    if kind == "payment_date":
        from app.fact_graph import facts_compatible as _fc

        if _fc(left, right):
            return _row(
                left,
                right,
                key,
                label,
                "compatible payment dates",
                "Payment dates are compatible.",
                False,
                False,
                confidence=40,
            )
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        materiality = (
            "Servicer payment-received dates disagree with bank debit dates for the same "
            "loan payment cycle — own-packet hazard / payment-plan credibility interrupt."
        )
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=84)
        row["documentary"] = True
        row["case_impact_bucket"] = "credibility"
        return row
    if kind == "entity":
        from app.fact_graph import facts_compatible as _fc

        if _fc(left, right):
            return _row(left, right, key, label, "same entity", "Entities match.", False, False, confidence=40)
        lv = str(left.get("value") or "").strip().lower()
        rv = str(right.get("value") or "").strip().lower()
        if lv == rv:
            return _row(left, right, key, label, "same entity", "Entities match.", False, False, confidence=40)
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        if key == "employer_of_record":
            materiality = (
                "Named employer on the service narrative cannot both be true with a different "
                "employer of record in deposition / employment verification for the same person."
            )
            bucket = "credibility"
        else:
            materiality = (
                "Distinct grounded sale/deed grantee names across sale and deed papers "
                "are a credibility interrupt. Identical Highland-vs-Highland naming is not."
            )
            bucket = "credibility"
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=80)
        row["documentary"] = True
        row["case_impact_bucket"] = bucket
        return row
    if kind == "rate":
        a, b = float(left["value"]), float(right["value"])
        delta = abs(a - b)
        # Documentary note vs sought rate (graph)
        if key == "stated_interest_rate":
            from app.fact_graph import facts_compatible as _fc

            if _fc(left, right):
                return _row(left, right, key, label, "same rate", "Rates match.", False, False, confidence=40)
            discrepancy = f"{left.get('display')} vs {right.get('display')} ({delta:.3f} points)"
            materiality = (
                "Note/contract interest rate cannot both be true with a different rate "
                "sought in the motion/judgment — damages / credibility interrupt (not ordinary "
                "default/penalty rate movement)."
            )
            row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=88)
            row["documentary"] = True
            row["case_impact_bucket"] = "damages"
            return row
        over = delta > meta.get("abs", 0.1)
        borderline = (not over) and delta > 0.05
        discrepancy = f"{delta:.3f} percentage points"
        materiality = f"Interest rate flags when it differs by more than {meta.get('abs', 0.1)} points."
        return _row(left, right, key, label, discrepancy, materiality, over, borderline, confidence=88 if over else 58)
    if kind == "venue":
        from app.fact_graph import facts_compatible as _fc

        if _fc(left, right):
            return _row(left, right, key, label, "same venue", "Venues match.", False, False, confidence=40)
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        materiality = (
            "Same process server executing affidavits for the same case on the same day "
            "cannot swear both Suffolk and Kings venue — credibility interrupt."
        )
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=86)
        row["documentary"] = True
        row["case_impact_bucket"] = "credibility"
        return row
    if kind == "year":
        if str(left.get("value")) == str(right.get("value")):
            return _row(left, right, key, label, "same year", "Years match.", False, False, confidence=40)
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        materiality = f"{label} flags when the year differs for the same person/employer."
        return _row(left, right, key, label, discrepancy, materiality, True, False, confidence=80)
    if kind == "address":
        if str(left.get("value")) == str(right.get("value")):
            return _row(left, right, key, label, "same address", "Addresses match.", False, False, confidence=40)
        discrepancy = f"{left.get('display')} vs {right.get('display')}"
        materiality = (
            "Same person with incompatible residence/occupancy addresses in their own "
            "papers is an element/credibility interrupt (primary residence vs license/tax address)."
        )
        row = _row(left, right, key, label, discrepancy, materiality, True, False, confidence=84)
        row["documentary"] = True
        row["case_impact_bucket"] = "element"
        return row
    if kind == "date":
        if left.get("value") == right.get("value"):
            return _row(left, right, key, label, "same date", "Dates match.", False, False, confidence=40)
        same_month = left.get("month_key") and left.get("month_key") == right.get("month_key")
        deadline = bool(meta.get("deadline") or left.get("deadline") or key == "date_of_birth")
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
        if key == "date_of_birth":
            materiality = (
                "Date of birth cannot differ across the party's own documents — "
                "identity conflict is a credibility interrupt."
            )
        elif key == "occupancy_start":
            materiality = (
                "Same person with incompatible occupancy-start dates in their own "
                "affidavits / pleadings is a credibility interrupt."
            )
        elif left.get("documentary") or right.get("graph") or left.get("graph"):
            materiality = (
                "Same person/employer with incompatible employment dates in documentary "
                "exhibits is a credibility interrupt."
            )
        else:
            materiality = (
                "Date flags when the month or year differs, or when a notice/foreclosure deadline day does not match."
            )
        row = _row(left, right, key, label, discrepancy, materiality, over, False, confidence=76 if over else 40)
        if left.get("documentary") or right.get("graph") or left.get("graph") or key == "date_of_birth":
            row["documentary"] = True
        if key == "occupancy_start" and over:
            row["documentary"] = True
            row["case_impact_bucket"] = "credibility"
        return row
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


def _gotcha_cluster_key(item: dict[str, Any]) -> tuple[str, ...]:
    """One card per fact theme (service at an address, settlement date), not per sentence."""
    if item.get("tainted_support") and item.get("document_id"):
        return ("tainted", str(item.get("document_id")))
    blob = f"{item.get('claim_quote') or ''} {item.get('evidence_quote') or ''}".lower()
    dates = tuple(sorted(set(re.findall(r"\b(?:\d{1,2}/\d{1,2}/\d{2,4}|(?:19|20)\d{2})\b", blob))))[:2]
    nums = tuple(sorted(set(re.findall(r"\b\d{3,5}\b", blob))))[:2]
    if re.search(r"\b(serv(?:e|ed|ice)|summons|process\s+server|mailings?)\b", blob):
        return ("service",) + dates + nums
    if re.search(r"\b(settlement|conference)\b", blob):
        return ("settlement",) + dates + nums
    if re.search(r"\b(resid|lived|address)\b", blob) and nums:
        return ("residence",) + nums
    fact = str(item.get("fact_named") or "").strip().lower()[:80]
    if fact:
        return ("fact", fact)
    return (
        "quote",
        _quote_key(str(item.get("claim_quote") or item.get("title") or "")),
        _quote_key(str(item.get("evidence_quote") or "")),
    )


def dedupe_gotchas(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One card per claim/evidence fact; later hits add related documents."""
    grouped: dict[tuple[str, ...], dict[str, Any]] = {}
    order: list[tuple[str, ...]] = []
    for item in items:
        key = _gotcha_cluster_key(item)
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
    # Same recording-number scoping as compare_to_case: a bundled exhibit
    # citing a different CRFN than the Complaint describes a different loan,
    # so its money facts should not be pooled with the case's operative
    # loan_amount/monthly_payment/outstanding_balance figures. See the
    # confirmed false positive this fixed: a Wells Fargo "Mortgages" exhibit
    # on an unrelated lien/property produced three spurious "does not match
    # across the case" high-risk cards for the real U.S. Bank loan.
    complaint_crfns: set[str] = set()
    for snap in snapshots:
        name = snap.get("display_name") or snap.get("original_filename") or ""
        if _looks_like_complaint(name):
            complaint_crfns |= _crfns_in(snap.get("text_excerpt") or "")

    # Soft-cluster same nameable facts (person-only merges with person|employer).
    by_key: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    for snap in snapshots:
        snap_crfns = _crfns_in(snap.get("text_excerpt") or "") if complaint_crfns else set()
        excluded_money = bool(complaint_crfns and snap_crfns and not (snap_crfns & complaint_crfns))
        for fact in snap.get("facts") or []:
            key = str(fact.get("key") or "")
            if key not in GOTCHA_KEYS and not fact.get("graph"):
                continue
            if excluded_money and fact.get("kind") == "money":
                continue
            by_key.setdefault(cluster_key(fact), []).append((snap, fact))

    cluster_ids = list(by_key.keys())
    merged: dict[str, list[tuple[dict[str, Any], dict[str, Any]]]] = {}
    assigned: set[str] = set()
    for i, ck in enumerate(cluster_ids):
        if ck in assigned:
            continue
        bucket = list(by_key[ck])
        assigned.add(ck)
        sample = bucket[0][1]
        for other_ck in cluster_ids[i + 1 :]:
            if other_ck in assigned:
                continue
            other_sample = by_key[other_ck][0][1]
            if same_nameable_fact(sample, other_sample):
                bucket.extend(by_key[other_ck])
                assigned.add(other_ck)
        merged[ck] = bucket

    out: list[dict[str, Any]] = []
    # Pack-level gate for unexplained arrears drop (702161 P002).
    from app.fact_graph import unexplained_arrears_drop

    zero_payment_activity = any(
        str(f.get("key")) == "loan_payment_activity" and str(f.get("value")) == "none"
        for snap in snapshots
        for f in (snap.get("facts") or [])
    )
    for ck, pairs in merged.items():
        variants: dict[str, dict[str, Any]] = {}
        for snap, fact in pairs:
            token = f"{fact.get('key')}:{fact.get('value')}"
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
        impact_bucket = None
        unique_facts = [bucket["fact"] for bucket in variants.values()]
        key = str(unique_facts[0].get("key") or ck.split("::", 1)[0])
        for idx, left in enumerate(unique_facts):
            for right in unique_facts[idx + 1 :]:
                row = None
                if (
                    key == "claimed_arrears"
                    and zero_payment_activity
                    and unexplained_arrears_drop(left, right)
                ):
                    try:
                        a, b = float(left["value"]), float(right["value"])
                        delta = abs(a - b)
                        pct = delta / max(a, b, 1.0)
                        discrepancy = (
                            f"${delta:,.2f} ({pct:.1%}) from {left.get('on_date')} "
                            f"to {right.get('on_date')}"
                        )
                    except (TypeError, ValueError, KeyError):
                        discrepancy = f"{left.get('display')} vs {right.get('display')}"
                    row = _row(
                        left,
                        right,
                        key,
                        "Claimed arrears / unpaid aggregate",
                        discrepancy,
                        (
                            "Claimed arrears dropped sharply across named complaint dates while "
                            "the servicing history shows no payments received — damages interrupt."
                        ),
                        True,
                        False,
                        confidence=90,
                    )
                    row["documentary"] = True
                    row["case_impact_bucket"] = "damages"
                elif not facts_compatible(left, right):
                    row = _score_graph_or_meta(left, right)
                elif left.get("key") == right.get("key"):
                    row = _score_pair(left, right, key)
                if row and row.get("flag"):
                    flagged = True
                    if row.get("case_impact_bucket"):
                        impact_bucket = row.get("case_impact_bucket")
                    if row.get("severity") == "high":
                        worst = "high"
                        confidence = max(confidence, int(row.get("confidence") or 70))
        if not flagged:
            continue
        meta = FACT_META.get(key) or {}
        label = str(meta.get("label") or unique_facts[0].get("label") or key)
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
        documentary = bool(unique_facts[0].get("documentary") or unique_facts[0].get("graph"))
        out.append(
            {
                "id": "fact-" + hashlib.sha1(ck.encode("utf-8")).hexdigest()[:10],
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
                "case_impact": documentary,
                "case_impact_bucket": impact_bucket or ("credibility" if documentary else None),
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
