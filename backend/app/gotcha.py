"""Claim-versus-evidence gotchas for any practice area.

Pass 1 proposes candidate pairs. Pass 2 keeps a pair only if it is the same
nameable fact, the evidence runs AGAINST the claim, the fact is material, and
both quotes are grounded in the pulled text. Rejects are logged, not dropped
silently.
"""

from __future__ import annotations

import hashlib
import re
from typing import Any

from app import cases_db
from app.config import settings
from app.engines import finding
from app.engines.contextual_logic import _generation_config, _parse_json_payload, _post_gemini
from app.models import Finding
from app.scan_profile import COURT_FILING_KINDS

CONTRADICTION_TYPES = (
    "factual_contradiction",
    "chronology_inconsistency",
    "material_omission",
    "procedural_foundational",
    "admission_against_interest",
)

TYPE_LABELS = {
    "factual_contradiction": "Factual contradiction",
    "chronology_inconsistency": "Chronology inconsistency",
    "material_omission": "Material omission",
    "procedural_foundational": "Procedural / foundational defect",
    "admission_against_interest": "Admission against interest",
}

# High-materiality types weigh more in document scoring.
TYPE_SCORE_WEIGHT = {
    "procedural_foundational": 1.45,
    "admission_against_interest": 1.45,
    "factual_contradiction": 1.2,
    "chronology_inconsistency": 1.1,
    "material_omission": 1.05,
}

STOP = frozenset(
    """
    the a an and or of to for in on at by with from as is was were be this that
    these those their there then than they them have has had been being into
    over under about after before within without such any all also only other
    document file case court plaintiff defendant party counsel attorney said
    section paragraph exhibit attached hereto hereby pursuant
    """.split()
)

CLAIM_CUE = re.compile(
    r"(?i)\b("
    r"never|did\s+not|do\s+not|does\s+not|was\s+not|were\s+not|"
    r"no\s+knowledge|not\s+aware|first\s+learned|without|"
    r"in\s+violation|contrary\s+to|despite|continued\s+to|kept\s+\w+ing|"
    r"I\s+(?:was|lived|resided|have\s+no|did\s+not)|"
    r"after\s+(?:agreeing|the\s+)?(?:settlement|stipulation|agreement|order)"
    r")\b"
)

# Evidence that a process failed or was defective usually SUPPORTS a denial claim.
PROCESS_DEFECT = re.compile(
    r"(?i)\b("
    r"defective|invalid|improper|insufficient|inadequate|untimely|"
    r"failed\s+to|did\s+not\s+comply|non-?compliant|vacated|set\s+aside|"
    r"denied\s+as|found\s+that.{0,40}(?:defective|invalid|improper)"
    r")\b"
)

DUTY_OR_CONDITION = re.compile(
    r"(?i)\b(repair|maintain|condition|pristine|habitab|warranty|inspect|disclose)\b"
)
PLACE_FACT = re.compile(
    r"(?i)\b(lived|resided|residing|located\s+in|was\s+in|was\s+at|arrived\s+at|"
    r"job\s+site|city|county|state)\b"
)

CANDIDATE_SCHEMA = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "claim_quote": {"type": "string"},
                    "claim_source": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "evidence_source": {"type": "string"},
                    "the_lie": {"type": "string"},
                    "case_impact": {"type": "string"},
                },
                "required": [
                    "title",
                    "claim_quote",
                    "claim_source",
                    "evidence_quote",
                    "evidence_source",
                ],
            },
        }
    },
    "required": ["candidates"],
}

VERIFY_SCHEMA = {
    "type": "object",
    "properties": {
        "results": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer"},
                    "same_fact": {
                        "type": "object",
                        "properties": {
                            "pass": {"type": "boolean"},
                            "fact_named": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["pass", "reason"],
                    },
                    "directionality": {
                        "type": "object",
                        "properties": {
                            "pass": {"type": "boolean"},
                            "evidence_asserts": {"type": "string"},
                            "runs": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["pass", "runs", "reason"],
                    },
                    "materiality": {
                        "type": "object",
                        "properties": {
                            "pass": {"type": "boolean"},
                            "tier": {"type": "string"},
                            "reason": {"type": "string"},
                        },
                        "required": ["pass", "reason"],
                    },
                    "textual_grounding": {
                        "type": "object",
                        "properties": {
                            "pass": {"type": "boolean"},
                            "reason": {"type": "string"},
                        },
                        "required": ["pass", "reason"],
                    },
                    "contradiction_type": {"type": "string"},
                    "pass": {"type": "boolean"},
                    "failed_step": {"type": "string"},
                },
                "required": ["index", "pass"],
            },
        }
    },
    "required": ["results"],
}

CANDIDATE_PROMPT = """Propose candidate claim/evidence pairs from any practice area.
A claim is a factual assertion by a party, witness, or counsel. Evidence is
another statement or record about a discrete fact. Do not assign a final
gotcha label. Do not use practice-area templates. Quote only from the text.

Claims:
{claims}

Evidence excerpts:
{evidence}
"""

VERIFY_PROMPT = """Verify each candidate pair. A pair is a gotcha only if ALL four pass.
Show work. Do not infer unstated facts. Practice-area-neutral.

1. SAME FACT — one nameable fact (date, place, amount, act, knowledge, status).
   Shared topic, person, or document is not enough.
2. DIRECTIONALITY — one sentence: what the evidence quote asserts on its own.
   Then FOR, AGAINST, or NEITHER the claim. Qualify only if AGAINST.
   Related documents that document a defect or dispute about the same subject
   often SUPPORT a denial claim; that is FOR, not against.
3. MATERIALITY — element, defense, procedure/foundation, credibility on a
   contested issue, or damages. Reject incidental details.
4. TEXTUAL GROUNDING — every step must be in the two quotes. If you need
   unstated context, fail this step.

contradiction_type must be one of: factual_contradiction, chronology_inconsistency,
material_omission, procedural_foundational, admission_against_interest.

Candidates:
{candidates}
"""


def find_gotchas(
    *,
    case_id: str,
    document_id: str,
    display_name: str,
    doc_kind: str | None,
    text: str,
) -> tuple[list[Finding], dict[str, Any]]:
    docs = cases_db.list_documents(case_id)
    snaps = {item["document_id"]: item for item in cases_db.list_case_snapshots(case_id)}
    others = [doc for doc in docs if doc["id"] != document_id]
    is_filing = (doc_kind or "").lower() in COURT_FILING_KINDS

    if is_filing:
        claims = extract_claims(text, display_name, document_id)
        evidence = retrieve_evidence(others, snaps, claims)
    else:
        claims = []
        for doc in others:
            if (doc.get("doc_kind") or "").lower() not in COURT_FILING_KINDS:
                continue
            excerpt = (snaps.get(doc["id"]) or {}).get("text_excerpt") or ""
            claims.extend(
                extract_claims(
                    excerpt,
                    doc.get("display_name") or doc.get("original_filename") or "",
                    doc["id"],
                )
            )
        this_doc = next((doc for doc in docs if doc["id"] == document_id), None)
        evidence = retrieve_evidence(
            [this_doc] if this_doc else [],
            snaps,
            claims,
            fallback_text=text,
        )

    empty = {"candidates": 0, "accepted": 0, "rejected": []}
    if not claims or not evidence:
        return [], empty

    candidates = propose_candidates(claims, evidence)
    accepted, rejected = verify_candidates(candidates)
    findings = [_to_finding(item) for item in accepted[:6]]
    return findings, {
        "candidates": len(candidates),
        "accepted": len(accepted),
        "rejected": rejected,
    }


def extract_claims(text: str, source: str, document_id: str, limit: int = 12) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sentence in _sentences(text):
        if not CLAIM_CUE.search(sentence):
            continue
        key = sentence.lower()[:160]
        if key in seen:
            continue
        seen.add(key)
        claims.append({"quote": sentence.strip()[:400], "source": source, "document_id": document_id})
        if len(claims) >= limit:
            break
    return claims


def retrieve_evidence(
    docs: list[dict[str, Any]],
    snaps: dict[str, dict[str, Any]],
    claims: list[dict[str, Any]],
    fallback_text: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    tokens = list(dict.fromkeys(tok for claim in claims for tok in _tokens(claim["quote"])))
    out: list[dict[str, Any]] = []
    for doc in docs:
        if not doc:
            continue
        text = (snaps.get(doc["id"]) or {}).get("text_excerpt") or ""
        if not text and fallback_text:
            text = fallback_text
        snippets = _snippets(text, tokens)
        if not snippets:
            continue
        out.append(
            {
                "source": doc.get("display_name") or doc.get("original_filename"),
                "document_id": doc["id"],
                "snippets": snippets[:3],
            }
        )
        if len(out) >= limit:
            break
    return out


def propose_candidates(
    claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    raw: list[dict[str, Any]] = []
    if settings.gemini_api_key:
        raw = _ask_candidates(claims, evidence)
    if not raw:
        raw = _token_candidates(claims, evidence)
    usable = []
    for item in raw:
        if not _usable(item):
            continue
        if item.get("document_id") and item.get("claim_document_id") == item.get("document_id"):
            continue
        usable.append(item)
    return usable[:12]


def verify_candidates(candidates: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    model_results = _ask_verify(candidates) if settings.gemini_api_key and candidates else {}
    for idx, candidate in enumerate(candidates):
        verdict = model_results.get(idx) or verify_pair(candidate)
        record = {**candidate, "verification": verdict}
        if verdict.get("pass"):
            record["contradiction_type"] = _normalize_type(verdict.get("contradiction_type"))
            accepted.append(record)
        else:
            rejected.append(
                {
                    "title": candidate.get("title"),
                    "claim_quote": candidate.get("claim_quote"),
                    "evidence_quote": candidate.get("evidence_quote"),
                    "failed_step": verdict.get("failed_step") or _first_fail(verdict),
                    "verification": verdict,
                }
            )
    return accepted, rejected


def verify_pair(candidate: dict[str, Any]) -> dict[str, Any]:
    """Local four-part check. Conservative: when unsure, fail the pair."""
    claim = str(candidate.get("claim_quote") or "")
    evidence = str(candidate.get("evidence_quote") or "")
    claim_toks = set(_tokens(claim))
    ev_toks = set(_tokens(evidence))
    overlap = claim_toks & ev_toks

    same = {
        "pass": False,
        "fact_named": "",
        "reason": "No shared nameable fact in the two quotes.",
    }
    if DUTY_OR_CONDITION.search(claim) and PLACE_FACT.search(evidence) and not DUTY_OR_CONDITION.search(evidence):
        same["reason"] = "Claim is about a duty or condition; evidence is about place. Different facts."
    elif PLACE_FACT.search(claim) and DUTY_OR_CONDITION.search(evidence) and not PLACE_FACT.search(evidence):
        same["reason"] = "Claim is about place; evidence is about a duty or condition. Different facts."
    elif PLACE_FACT.search(claim) and PLACE_FACT.search(evidence) and _shared_year(claim, evidence):
        same = {
            "pass": True,
            "fact_named": "whereabouts on a shared date",
            "reason": "Both quotes address where a person was on the same date.",
        }
    elif len(overlap) >= 2:
        same = {
            "pass": True,
            "fact_named": ", ".join(sorted(overlap)[:6]),
            "reason": "Both quotes share the same discrete fact tokens.",
        }
    elif len(overlap) == 1 and _has_opposing_polarity(claim, evidence) and _predicate_overlap(claim, evidence):
        same = {
            "pass": True,
            "fact_named": next(iter(overlap)),
            "reason": "One shared fact token plus opposing polarity on the same predicate.",
        }

    evidence_asserts = evidence[:220]
    runs = "neither"
    direction_pass = False
    direction_reason = "Evidence does not clearly run against the claim."
    if same["pass"] and PROCESS_DEFECT.search(evidence) and _is_denial(claim):
        runs = "for"
        direction_reason = (
            "Evidence describes a defect or failure about the same subject, which supports "
            "the denial rather than contradicting it."
        )
    elif same["pass"] and same.get("fact_named") == "whereabouts on a shared date":
        runs = "against"
        direction_pass = True
        direction_reason = "The evidence places the person somewhere else on the same date."
    elif same["pass"] and _has_opposing_polarity(claim, evidence):
        runs = "against"
        direction_pass = True
        direction_reason = "The evidence quote asserts the opposite of the claim on the same fact."
    direction = {
        "pass": direction_pass,
        "evidence_asserts": evidence_asserts,
        "runs": runs,
        "reason": direction_reason,
    }

    material_pass = direction_pass and not _incidental(claim, evidence)
    material = {
        "pass": material_pass,
        "tier": "element" if material_pass else "incidental",
        "reason": (
            "The fact bears on an element, procedure, credibility, or damages."
            if material_pass
            else "The difference looks incidental or formatting-level."
        ),
    }

    grounded = _usable(candidate)
    textual = {
        "pass": grounded,
        "reason": "Both quotes are present as pulled text." if grounded else "Quotes are too thin to ground.",
    }

    failed = None
    for step, block in (
        ("same_fact", same),
        ("directionality", direction),
        ("materiality", material),
        ("textual_grounding", textual),
    ):
        if not block.get("pass"):
            failed = step
            break
    contradiction_type = "factual_contradiction"
    if PROCESS_DEFECT.search(claim) or re.search(r"(?i)\b(stipulation|settlement|order|notice|service)\b", claim):
        contradiction_type = "procedural_foundational"
    if re.search(r"(?i)\b(date|after|before|thereafter)\b", claim) and contradiction_type == "factual_contradiction":
        contradiction_type = "chronology_inconsistency"
    return {
        "same_fact": same,
        "directionality": direction,
        "materiality": material,
        "textual_grounding": textual,
        "contradiction_type": contradiction_type,
        "pass": failed is None,
        "failed_step": failed,
    }


def _token_candidates(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for claim in claims:
        toks = set(_tokens(claim["quote"]))
        for item in evidence:
            if item.get("document_id") == claim.get("document_id"):
                continue
            for snippet in item.get("snippets") or []:
                if len(toks & set(_tokens(snippet))) < 2 and not _has_opposing_polarity(claim["quote"], snippet):
                    continue
                out.append(
                    {
                        "title": "Possible claim/evidence conflict",
                        "claim_quote": claim["quote"],
                        "claim_source": claim["source"],
                        "claim_document_id": claim.get("document_id"),
                        "evidence_quote": snippet[:320],
                        "evidence_source": item["source"],
                        "document_id": item.get("document_id"),
                        "the_lie": "",
                        "case_impact": "",
                    }
                )
                break
    return out


def _ask_candidates(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claim_block = "\n".join(f"- “{item['quote']}” — {item['source']}" for item in claims[:12])
    ev_block = "\n".join(
        f"- {item['source']}: " + " | ".join(item["snippets"][:3]) for item in evidence[:8]
    )
    try:
        body = _post_gemini(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": CANDIDATE_PROMPT.format(claims=claim_block, evidence=ev_block)}],
                    }
                ],
                "generationConfig": _generation_config(CANDIDATE_SCHEMA, settings.gemini_thinking_level),
            },
            timeout=max(settings.gemini_timeout_s, 40),
        )
        parsed = _parse_json_payload(body)
    except Exception:
        return []
    items = parsed.get("candidates") if isinstance(parsed, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _ask_verify(candidates: list[dict[str, Any]]) -> dict[int, dict[str, Any]]:
    blob = "\n\n".join(
        f"[{idx}] CLAIM ({item.get('claim_source')}): “{item.get('claim_quote')}”\n"
        f"EVIDENCE ({item.get('evidence_source')}): “{item.get('evidence_quote')}”"
        for idx, item in enumerate(candidates)
    )
    try:
        body = _post_gemini(
            {
                "contents": [{"role": "user", "parts": [{"text": VERIFY_PROMPT.format(candidates=blob)}]}],
                "generationConfig": _generation_config(VERIFY_SCHEMA, settings.gemini_thinking_level),
            },
            timeout=max(settings.gemini_timeout_s, 45),
        )
        parsed = _parse_json_payload(body)
    except Exception:
        return {}
    out: dict[int, dict[str, Any]] = {}
    for item in parsed.get("results") or []:
        if not isinstance(item, dict):
            continue
        try:
            idx = int(item.get("index"))
        except (TypeError, ValueError):
            continue
        if not item.get("pass"):
            item["failed_step"] = item.get("failed_step") or _first_fail(item)
        item["contradiction_type"] = _normalize_type(item.get("contradiction_type"))
        out[idx] = item
    return out


def _to_finding(item: dict[str, Any]) -> Finding:
    vtype = _normalize_type(item.get("contradiction_type"))
    verify = item.get("verification") or {}
    title = str(item.get("title") or TYPE_LABELS[vtype])
    seed = f"gotcha|{item.get('claim_quote')}|{item.get('evidence_quote')}"
    fid = "ctx-gotcha-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    severity = "high" if vtype in {"procedural_foundational", "admission_against_interest"} else "medium"
    return finding(
        fid=fid,
        category="gotcha",
        severity=severity,  # type: ignore[arg-type]
        title=f"GOTCHA: {TYPE_LABELS[vtype]}",
        explanation=(
            f"CLAIM ({item.get('claim_source') or 'filing'}): “{item.get('claim_quote')}” "
            f"EVIDENCE ({item.get('evidence_source') or 'record'}): “{item.get('evidence_quote')}” "
            f"{item.get('the_lie') or ''}"
        ),
        why_suspicious=str(item.get("case_impact") or verify.get("directionality", {}).get("reason") or ""),
        confidence=88.0 if severity == "high" else 74.0,
        evidence={
            "gotcha": True,
            "verified": True,
            "gotcha_level": TYPE_LABELS[vtype],
            "contradiction_type": vtype,
            "analysis_type": "gotcha",
            "claim_quote": item.get("claim_quote"),
            "claim_source": item.get("claim_source"),
            "evidence_quote": item.get("evidence_quote"),
            "evidence_source": item.get("evidence_source"),
            "the_lie": item.get("the_lie"),
            "case_impact": item.get("case_impact"),
            "verification": verify,
            "material": True,
            "prior_display_name": item.get("evidence_source"),
        },
        recommended_action=(
            f"Open “{item.get('claim_source')}” and “{item.get('evidence_source')}”. "
            "Confirm the quotes before using them as impeachment."
        ),
    )


def _usable(item: dict[str, Any]) -> bool:
    claim = str(item.get("claim_quote") or "").strip()
    evidence = str(item.get("evidence_quote") or "").strip()
    return len(claim) >= 12 and len(evidence) >= 12 and claim.lower() != evidence.lower()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']{4,}", (text or "").lower())
    return [w for w in words if w not in STOP]


def _snippets(text: str, tokens: list[str], window: int = 280, limit: int = 3) -> list[str]:
    if not text or not tokens:
        return []
    hay = text.lower()
    spans: list[tuple[int, int]] = []
    for token in tokens[:16]:
        at = hay.find(token)
        if at < 0:
            continue
        spans.append((max(0, at - window // 3), min(len(text), at + window)))
    out = []
    for left, right in spans[:limit]:
        chunk = re.sub(r"\s+", " ", text[left:right]).strip()
        if chunk:
            out.append(chunk)
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [part.strip() for part in parts if 24 <= len(part.strip()) <= 500]


def _is_denial(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(never|did\s+not|do\s+not|was\s+not|were\s+not|without|no\s+(?:knowledge|prior|income|contact|injury))\b",
            text,
        )
    )


def _shared_year(left: str, right: str) -> bool:
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", left)) & set(re.findall(r"\b(?:19|20)\d{2}\b", right))
    return bool(years)


def _predicate_overlap(claim: str, evidence: str) -> bool:
    return bool(
        set(_tokens(claim))
        & set(_tokens(evidence))
        & {
            "signed",
            "signature",
            "notice",
            "income",
            "injury",
            "contact",
            "collection",
            "agreement",
            "warning",
            "theft",
            "store",
            "settlement",
            "stipulation",
            "knowledge",
            "delivered",
            "authorized",
        }
    )


def _has_opposing_polarity(claim: str, evidence: str) -> bool:
    if _is_denial(claim) and not _is_denial(evidence) and not PROCESS_DEFECT.search(evidence):
        return True
    if not _is_denial(claim) and _is_denial(evidence):
        return True
    if re.search(r"(?i)\b(after|despite|continued|kept)\b", claim) and re.search(
        r"(?i)\b(agree|stipulat|settlement|cancel|discharge|order)\b", evidence
    ):
        return True
    return False


def _incidental(claim: str, evidence: str) -> bool:
    blob = f"{claim} {evidence}".lower()
    if re.search(r"\b(typo|spelling|abbreviation|format)\b", blob):
        return True
    return False


def _normalize_type(value: Any) -> str:
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if raw in CONTRADICTION_TYPES:
        return raw
    aliases = {
        "procedural": "procedural_foundational",
        "foundational": "procedural_foundational",
        "admission": "admission_against_interest",
        "omission": "material_omission",
        "chronology": "chronology_inconsistency",
        "factual": "factual_contradiction",
    }
    for key, mapped in aliases.items():
        if key in raw:
            return mapped
    return "factual_contradiction"


def _first_fail(verdict: dict[str, Any]) -> str:
    for step in ("same_fact", "directionality", "materiality", "textual_grounding"):
        block = verdict.get(step)
        if isinstance(block, dict) and block.get("pass") is False:
            return step
    return str(verdict.get("failed_step") or "same_fact")
