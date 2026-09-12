"""Find claim-versus-evidence lies — not rate rounding or dated-vs-recorded chronology.

A gotcha is: a party said X in a pleading or affidavit, and another document
in the case is hard evidence that X is false.
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

CLAIM_PATTERNS: list[tuple[str, re.Pattern[str]]] = [
    (
        "no_notice",
        re.compile(
            r"(?i)(never\s+received|did\s+not\s+receive|without\s+(?:prior\s+)?notice|"
            r"no\s+(?:90[\s-]?day\s+)?notice\s+was\s+(?:given|served|mailed)|"
            r"was\s+not\s+(?:served|notified)|never\s+served)"
        ),
    ),
    (
        "no_income",
        re.compile(
            r"(?i)(no\s+income|without\s+income|unemployed|lost\s+(?:my\s+)?job|"
            r"have\s+no\s+assets|cannot\s+pay)"
        ),
    ),
    (
        "no_knowledge",
        re.compile(
            r"(?i)(no\s+knowledge|never\s+knew|was\s+not\s+aware|never\s+(?:told|informed)|"
            r"first\s+learned|did\s+not\s+know)"
        ),
    ),
    (
        "did_not_sign",
        re.compile(
            r"(?i)(did\s+not\s+sign|not\s+my\s+signature|signature\s+is\s+(?:a\s+)?forgery|"
            r"never\s+executed|I\s+never\s+signed)"
        ),
    ),
    (
        "never_met",
        re.compile(r"(?i)(never\s+met|do\s+not\s+know\s+(?:the\s+)?defendant|no\s+relationship\s+with)"),
    ),
    (
        "residence",
        re.compile(
            r"(?i)((?:resid(?:ed|ing)|lived|was\s+living)\s+in\s+[A-Z][A-Za-z .]{2,40}|"
            r"throughout\s+(?:January|February|March|April|May|June|July|August|September|October|November|December))"
        ),
    ),
]

EVIDENCE_PATTERNS: dict[str, re.Pattern[str]] = {
    "no_notice": re.compile(
        r"(?i)(certified\s+mail|return\s+receipt|green\s+card|RPAPL\s+1304|"
        r"notice\s+of\s+(?:default|intent).{0,40}(?:mailed|served|sent)|"
        r"affidavit\s+of\s+(?:service|mailing)|signed\s+for)"
    ),
    "no_income": re.compile(
        r"(?i)(wages|salary|gross\s+income|adjusted\s+gross|form\s+W-?2|"
        r"direct\s+deposit|payroll|consulting)"
    ),
    "no_knowledge": re.compile(
        r"(?i)(letter\s+dated|email\s+dated|wrote\s+to|informed\s+you|"
        r"you\s+were\s+advised|prior\s+correspondence)"
    ),
    "did_not_sign": re.compile(
        r"(?i)(notarized|acknowledged\s+before|sworn\s+to\s+before|"
        r"signature\s+of|executed\s+before)"
    ),
    "never_met": re.compile(r"(?i)(email\s+from|wrote\s+to|meeting\s+with|spoke\s+with)"),
    "residence": re.compile(
        r"(?i)(purchase|transaction|store|Home Depot|Brooklyn|New York|"
        r"point\s+of\s+sale|in-store)"
    ),
}

GOTCHA_SCHEMA = {
    "type": "object",
    "properties": {
        "gotchas": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "party": {"type": "string"},
                    "claim_quote": {"type": "string"},
                    "claim_source": {"type": "string"},
                    "evidence_quote": {"type": "string"},
                    "evidence_source": {"type": "string"},
                    "evidence_type": {"type": "string"},
                    "the_lie": {"type": "string"},
                    "case_impact": {"type": "string"},
                    "confidence": {"type": "string"},
                },
                "required": [
                    "title",
                    "claim_quote",
                    "claim_source",
                    "evidence_quote",
                    "evidence_source",
                    "the_lie",
                    "case_impact",
                    "confidence",
                ],
            },
        }
    },
    "required": ["gotchas"],
}

GOTCHA_PROMPT = """You are a litigation lie detector. Return ONLY gotchas: a specific CLAIM
in a pleading/affidavit that hard EVIDENCE in another document proves false.

A gotcha requires ALL of these:
- Exact claim quote (unambiguous)
- Exact evidence quote from a different document
- The two cannot both be true
- Evidence is objective (statement, receipt, email, photo, tax record) — not opinion
- It could impeach the witness or win a point with the court
- No reasonable way both are true

DO NOT report: interest-rate rounding, dated vs recorded mortgages, name abbreviations,
balances off by under 1%, missing details, speculation, one person's word vs another
without a record, or anything an attorney would call a discrepancy rather than a lie.

If none qualify, return {{"gotchas":[]}}.

Claims:
{claims}

Evidence excerpts:
{evidence}
"""


def find_gotchas(
    *,
    case_id: str,
    document_id: str,
    display_name: str,
    doc_kind: str | None,
    text: str,
) -> list[Finding]:
    docs = cases_db.list_documents(case_id)
    snaps = {item["document_id"]: item for item in cases_db.list_case_snapshots(case_id)}
    others = [doc for doc in docs if doc["id"] != document_id]
    is_filing = (doc_kind or "").lower() in COURT_FILING_KINDS

    if is_filing:
        claims = extract_claims(text, display_name, document_id)
        evidence = _evidence_from_docs(others, snaps, claims)
    else:
        claims = []
        for doc in others:
            if (doc.get("doc_kind") or "").lower() not in COURT_FILING_KINDS:
                continue
            excerpt = (snaps.get(doc["id"]) or {}).get("text_excerpt") or ""
            claims.extend(extract_claims(excerpt, doc.get("display_name") or doc.get("original_filename") or "", doc["id"]))
        evidence = _evidence_from_docs(
            [next(d for d in docs if d["id"] == document_id)],
            snaps,
            claims,
            fallback_text=text,
        )

    if not claims or not evidence:
        return []

    raw: list[dict[str, Any]] = []
    if settings.gemini_api_key:
        raw = _ask_model(claims, evidence)
    if not raw:
        raw = _deterministic_gotchas(claims, evidence)
    return [_to_finding(item) for item in raw[:5] if _usable(item)]


def extract_claims(text: str, source: str, document_id: str, limit: int = 10) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    for sentence in _sentences(text):
        for kind, pattern in CLAIM_PATTERNS:
            if not pattern.search(sentence):
                continue
            key = sentence.lower()[:160]
            if key in seen:
                continue
            seen.add(key)
            claims.append(
                {
                    "kind": kind,
                    "quote": sentence.strip()[:400],
                    "source": source,
                    "document_id": document_id,
                }
            )
            if len(claims) >= limit:
                return claims
            break
    return claims


def _evidence_from_docs(
    docs: list[dict[str, Any]],
    snaps: dict[str, dict[str, Any]],
    claims: list[dict[str, Any]],
    fallback_text: str | None = None,
    limit: int = 8,
) -> list[dict[str, Any]]:
    kinds = {claim["kind"] for claim in claims}
    out: list[dict[str, Any]] = []
    for doc in docs:
        text = (snaps.get(doc["id"]) or {}).get("text_excerpt") or ""
        if doc["id"] and fallback_text and not text:
            text = fallback_text
        if not text:
            continue
        hits: list[str] = []
        for kind in kinds:
            pattern = EVIDENCE_PATTERNS.get(kind)
            if not pattern:
                continue
            for match in pattern.finditer(text):
                start = max(0, match.start() - 180)
                end = min(len(text), match.end() + 220)
                chunk = re.sub(r"\s+", " ", text[start:end]).strip()
                if chunk:
                    hits.append(chunk)
                if len(hits) >= 3:
                    break
        if not hits:
            continue
        out.append(
            {
                "source": doc.get("display_name") or doc.get("original_filename"),
                "document_id": doc["id"],
                "snippets": hits[:3],
            }
        )
        if len(out) >= limit:
            break
    return out


def _ask_model(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    claim_block = "\n".join(
        f"- ({item['kind']}) “{item['quote']}” — {item['source']}" for item in claims[:10]
    )
    ev_block = "\n".join(
        f"- {item['source']}: " + " | ".join(item["snippets"][:3]) for item in evidence[:8]
    )
    try:
        body = _post_gemini(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": GOTCHA_PROMPT.format(claims=claim_block, evidence=ev_block)}],
                    }
                ],
                "generationConfig": _generation_config(GOTCHA_SCHEMA, settings.gemini_thinking_level),
            },
            timeout=max(settings.gemini_timeout_s, 40),
        )
        parsed = _parse_json_payload(body)
    except Exception:
        return []
    items = parsed.get("gotchas") if isinstance(parsed, dict) else None
    return [item for item in items if isinstance(item, dict)] if isinstance(items, list) else []


def _deterministic_gotchas(
    claims: list[dict[str, Any]],
    evidence: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for claim in claims:
        pattern = EVIDENCE_PATTERNS.get(claim["kind"])
        if not pattern:
            continue
        for item in evidence:
            if item.get("document_id") == claim.get("document_id"):
                continue
            snippet = next((s for s in item.get("snippets") or [] if pattern.search(s)), None)
            if not snippet:
                continue
            out.append(
                {
                    "title": _title_for(claim["kind"]),
                    "party": "Claimant",
                    "claim_quote": claim["quote"],
                    "claim_source": claim["source"],
                    "evidence_quote": snippet[:280],
                    "evidence_source": item["source"],
                    "evidence_type": claim["kind"],
                    "the_lie": (
                        f"The claim “{claim['quote'][:160]}” cannot stand next to "
                        f"the record in “{item['source']}”."
                    ),
                    "case_impact": "This is impeachment material if the record is authentic.",
                    "confidence": "verify",
                }
            )
            break
    return out


def _title_for(kind: str) -> str:
    return {
        "no_notice": "Claimed no notice — record shows service or mailing",
        "no_income": "Claimed no income — record shows earnings or deposits",
        "no_knowledge": "Claimed no knowledge — correspondence shows they were told",
        "did_not_sign": "Denied signing — record shows a notarized or acknowledged signature",
        "never_met": "Claimed they never met — communications show contact",
        "residence": "Claimed they were elsewhere — a record places them on scene",
    }.get(kind, "Claim contradicted by case evidence")


def _usable(item: dict[str, Any]) -> bool:
    claim = str(item.get("claim_quote") or "").strip()
    evidence = str(item.get("evidence_quote") or "").strip()
    if len(claim) < 12 or len(evidence) < 12:
        return False
    if claim.lower() == evidence.lower():
        return False
    return True


def _to_finding(item: dict[str, Any]) -> Finding:
    level = str(item.get("confidence") or "verify").lower().replace(" ", "_")
    if "slam" in level:
        severity, confidence, material, badge = "high", 92.0, True, "SLAM DUNK"
    elif "strong" in level:
        severity, confidence, material, badge = "high", 80.0, True, "STRONG"
    else:
        severity, confidence, material, badge = "medium", 68.0, True, "GOTCHA"
    title = str(item.get("title") or "Gotcha").strip()
    seed = f"gotcha|{item.get('claim_quote')}|{item.get('evidence_quote')}"
    fid = "ctx-gotcha-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    return finding(
        fid=fid,
        category="gotcha",
        severity=severity,  # type: ignore[arg-type]
        title=f"GOTCHA: {title}",
        explanation=(
            f"CLAIM ({item.get('claim_source') or 'filing'}): “{item.get('claim_quote')}” "
            f"EVIDENCE ({item.get('evidence_source') or 'record'}): “{item.get('evidence_quote')}” "
            f"THE LIE: {item.get('the_lie')}"
        ),
        why_suspicious=str(item.get("case_impact") or "This contradiction can impeach the claimant."),
        confidence=confidence,
        evidence={
            "gotcha": True,
            "gotcha_level": badge,
            "analysis_type": "gotcha",
            "claim_quote": item.get("claim_quote"),
            "claim_source": item.get("claim_source"),
            "evidence_quote": item.get("evidence_quote"),
            "evidence_source": item.get("evidence_source"),
            "evidence_type": item.get("evidence_type"),
            "the_lie": item.get("the_lie"),
            "case_impact": item.get("case_impact"),
            "material": material,
            "prior_display_name": item.get("evidence_source"),
        },
        recommended_action=(
            f"Open “{item.get('claim_source')}” and “{item.get('evidence_source')}”. "
            "Confirm the quotes and use them as impeachment if the record is authentic."
        ),
    )


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [part.strip() for part in parts if 24 <= len(part.strip()) <= 500]
