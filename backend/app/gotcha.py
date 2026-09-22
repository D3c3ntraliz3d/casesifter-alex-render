"""Claim-versus-evidence gotchas for any practice area.

This is systematic pattern detection and contradiction spotting with evidence
fraud analysis. It is not argument synthesis: do not assemble a party's case,
do not pair competing recitals as impeachment, and do not write advocacy.

Pass 1 proposes candidate pairs. Pass 2 keeps a pair only if it is the same
nameable fact, the evidence runs AGAINST the claim, the fact is material, and
both quotes are grounded in the pulled text. Rejects are logged, not dropped
silently.

Every user-facing gotcha must survive this gate. A model "pass" without all
four completed steps is not enough. Legacy contradiction labels are either
migrated into the five-type taxonomy or retired.
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
from app.evidence_tier import (
    TIER_ADVOCACY,
    TIER_EVIDENCE,
    claim_tier,
    classify_tier,
    counter_label,
    evidence_is_probative,
    find_internal_evidence_defects,
    is_self_undercut,
)
from app.scan_profile import COURT_FILING_KINDS

CONTRADICTION_TYPES = (
    "factual_contradiction",
    "chronology_inconsistency",
    "material_omission",
    "procedural_foundational",
    "admission_against_interest",
)

TYPE_LABELS = {
    "factual_contradiction": "Apparent factual contradiction",
    "chronology_inconsistency": "Apparent chronology inconsistency",
    "material_omission": "Apparent material omission",
    "procedural_foundational": "Apparent procedural / foundational issue",
    "admission_against_interest": "Apparent admission against interest",
}

# Retired as shipped contradiction labels. A model that still emits these cannot
# bypass verification by wearing an old name.
RETIRED_TYPES = frozenset(
    {
        "logical_inconsistency",
        "location_contradiction",
        "location_inconsistency",
    }
)

# Old intra-document chronology label → generic verified type.
MIGRATED_TYPES = {
    "date_inconsistency": "chronology_inconsistency",
}

# High-materiality types weigh more in document scoring.
TYPE_SCORE_WEIGHT = {
    "procedural_foundational": 1.45,
    "admission_against_interest": 1.45,
    "factual_contradiction": 1.2,
    "chronology_inconsistency": 1.1,
    "material_omission": 1.05,
}

VERIFY_STEPS = ("same_fact", "directionality", "materiality", "textual_grounding")

STOP = frozenset(
    """
    the a an and or of to for in on at by with from as is was were be this that
    these those their there then than they them have has had been being into
    over under about after before within without such any all also only other
    document file case court plaintiff defendant party counsel attorney said
    section paragraph exhibit attached hereto hereby pursuant
    united states america laws postal service made make making person persons
    name names upon under such thereof hereby herein aforesaid aforesaidly
    custody care exclusive official depositor first class mail mailing
    """.split()
)

# Stems that look like a "shared fact" but are legal/English boilerplate (Mamiye AOS noise).
BOILERPLATE_STEMS = frozenset(
    {
        "united",
        "state",
        "america",
        "law",
        "postal",
        "service",
        "made",
        "make",
        "person",
        "name",
        "mail",
        "mailing",
        "custody",
        "care",
        "exclusive",
        "depositor",
        "official",
        "first",
        "class",
        "upon",
        "under",
    }
)

CLAIM_CUE = re.compile(
    r"(?i)\b("
    r"never|did\s+not|do\s+not|does\s+not|was\s+not|were\s+not|"
    r"no\s+knowledge|no\s+prior|no\s+other|not\s+aware|first\s+learned|without|"
    r"in\s+violation|contrary\s+to|despite|continued\s+to|kept\s+\w+ing|"
    r"I\s+(?:was|lived|resided|have\s+(?:been|no)|did\s+not|am\s+the|disclosed)|"
    r"after\s+(?:agreeing|the\s+)?(?:settlement|stipulation|agreement|order)|"
    r"always|every|exclusive|sole|disclosed\s+all|provided\s+all"
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
    r"(?i)\b(repair|maintain|condition|pristine|habitab|warranty|inspect)\b"
)
PLACE_FACT = re.compile(
    r"(?i)\b(lived|resided|residing|located\s+in|was\s+in|was\s+at|arrived\s+at|"
    r"job\s+site|address|street|city|county|state)\b"
)
EXCLUSIVE_OR_COMPLETE = re.compile(
    r"(?i)\b("
    r"never|always|every|entire|sole|exclusive|"
    r"no\s+prior|no\s+other|no\s+knowledge|none|"
    r"disclosed\s+all|provided\s+all|all\s+accounts"
    r")\b"
)
COUNTER_INSTANCE = re.compile(
    r"(?i)\b("
    r"dated|appears\s+at|not\s+listed|email|"
    r"picked\s+up|outbound|executed|bears|acknowledged|opened|"
    r"failed\s+again"
    r")\b"
)

# Bare "I was not served" vs a process server's completed-service recital is the
# ordinary traverse. A gotcha needs the claimant's own exhibit to undercut them.
SERVICE_FACT = re.compile(
    r"(?i)\b("
    r"serv(?:e|ed|ice)|process\s+server|summons|complaint|"
    r"mailings?|personal\s+jurisdiction|suitable\s+age|"
    r"first\s+class\s+mail|conspicuous"
    r")\b"
)
SERVICE_COMPLETED = re.compile(
    r"(?i)("
    r"(?:i\s+|1)?served\s+(?:the\s+)?(?:within\s+)?(?:summons|complaint|papers?)|"
    r"on\s+\d{1,2}\s*/\s*\d{1,2}.{0,160}served|"
    r"sent\s+a\s+copy\s+of\s+the\s+summons|"
    r"by\s+first\s+class\s+mail|"
    r"person\s+of\s+suitable\s+age|"
    r"affidavit\s+of\s+service"
    r")"
)
SETTLEMENT_NEG = re.compile(
    r"(?i)\b((?:case\s+)?not\s+settled|settlement\s+was\s+not\s+reached|"
    r"no\s+settlement\s+was\s+reached)\b"
)
RESIDENCE_ASSERT = re.compile(r"(?i)\b(reside[sd]?|residing|live[sd]?|living|dwelling)\b")
RESIDENCE_DENY = re.compile(
    r"(?i)\b("
    r"(?:did|do|does|was|were)\s+not\s+(?:live|living|reside|residing)|"
    r"never\s+(?:lived|resided|living)|"
    r"not\s+(?:my\s+)?(?:residence|home\s+address|dwelling)"
    r")\b"
)
INTERNAL_TAINTED = frozenset(
    {
        "math_error",
        "historical_impossibility",
        "date_inconsistency",
        "template_fabrication",
        "logo_chrome_mismatch",
    }
)
CAPTION_OR_FORM = re.compile(
    r"(?i)\b("
    r"index/?case\s+number|county\s+of|court\s+of\s+the\s+state|"
    r"plaintiff,?\s+vs\.?|un-?rep\s+parties|issue\s+joined|"
    r"request\s+for\s+preliminary\s+conference|"
    r"writ\s+of\s+habeas|poor\s+person\s+application|"
    r"other\s+\(specify\)|certificate\s+of\s+merit"
    r")\b"
)
MILITARY_FORM = re.compile(r"(?i)\b(military\s+service|active\s+military)\b")
MAILING_FORM = re.compile(
    r"(?i)\b(first\s+class|official\s+depositor|post-paid|personal\s+&\s+confidential)\b"
)
OPPORTUNITY_CHAR = re.compile(
    r"(?i)\b(opportunity|did\s+not\s+have\s+the\s+chance|never\s+(?:went|had)\s+the\s+opportunity)\b"
)
DOCKET_LINE = re.compile(
    r"(?i)\b(conference|adjourned|no\s+courtroom|appearance|foreclosure\s+pre-?\s*settlement)\b"
)
APPEARANCE_FACT = re.compile(
    r"(?i)\b((?:did|does|have)\s+not\s+(?:answer|appear)|failed\s+to\s+answer|"
    r"time\s+to\s+answer\s+has\s+expired)\b"
)
ROLE_OR_IDENTITY = re.compile(
    r"(?i)\b(i\s+am\s+(?:the\s+|a\s+)?(?:defendant|plaintiff|attorney|counsel)|"
    r"being\s+duly\s+sworn)\b"
)
EXHIBIT_TAG = re.compile(
    r"(?i)\b(?:exhibit|ex\.?)[\s._-]+([A-Z])(?:[\s._-]+(\d{1,4}))?\b"
)
BILL_OR_STATEMENT = re.compile(
    r"(?i)\b(utility|bill|statement|account\s+number|amount\s+due|invoice)\b"
)
PRODUCED_CUE = re.compile(
    r"(?i)\b("
    r"attach(?:ed|ing)|annexed|appended|herewith|hereto|"
    r"i\s+submit|submitted\s+herewith|as\s+proof|in\s+support|"
    r"i\s+produce"
    r")\b"
)
TAINTED_ANALYSIS = frozenset(
    {
        "math_error",
        "text_overlay",
        "graphic_manipulation",
        "historical_impossibility",
        "date_inconsistency",
        "template_fabrication",
        "logo_chrome_mismatch",
    }
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
                    "why_it_may_matter": {"type": "string"},
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
                "required": [
                    "index",
                    "pass",
                    "same_fact",
                    "directionality",
                    "materiality",
                    "textual_grounding",
                ],
            },
        }
    },
    "required": ["results"],
}

CANDIDATE_PROMPT = """Propose candidate claim/evidence pairs from any practice area.

Role: forensic scan only. Systematic pattern detection and contradiction spotting
with evidence-fraud analysis. Do not assemble a party's case. Do not write advocacy.
Do not pair competing recitals as if that were impeachment.

A claim is a factual assertion by a party, witness, or counsel. Evidence must be
a sworn, documentary, or entered-court record about a discrete fact — not another
motion, brief, proposed order, or attorney affirmation. Two opposing arguments
are not a pair. He-said-she-said (a denial versus the other side's competing
recital, including a bare service denial versus an affidavit of service, or a
service-attempt claim versus a DMV/address printout that lists or supports the
same address) is not a pair. A pair is worth proposing when the claimant produced
the record and that record appears false or internally defective. Do not pair
a claim with a quote that restates it. Do not assign a final gotcha label.
Quote only from the text.

Claims:
{claims}

Evidence excerpts:
{evidence}
"""

VERIFY_PROMPT = """Verify each candidate pair. Forensic scan only — not argument synthesis.
A pair is a gotcha only if ALL four pass. Show work on every step. Do not infer
unstated facts. Practice-area-neutral. If any step is uncertain, fail that step.
Prefer reject over accept. Do not assemble a party's case or treat ordinary
dispute as impeachment.

1. SAME FACT — one nameable fact (date, place, amount, act, knowledge, status).
   Shared topic, person, party name, or caption is not enough. A form checkbox
   or appearance-calendar line is not the same fact as a party's characterization
   of whether they had a meaningful chance to participate.
2. DIRECTIONALITY — one sentence: what the evidence quote asserts on its own.
   Then FOR, AGAINST, or NEITHER the claim. Qualify only if AGAINST.
   Related documents that document a defect or dispute about the same subject
   often SUPPORT a denial claim; that is FOR, not against.
   If the evidence restates or corroborates the claim (for example "not settled"
   versus "case not settled", or a residence claim versus a record listing that
   same address), that is FOR, not against.
   He-said-she-said is not a gotcha: a denial versus the other side's competing
   recital, with no produced record that is false or fraudulent. Mark that FOR
   or NEITHER. A gotcha is when they produce evidence and that evidence is
   fraudulent or false — for example they deny living at an address and the
   exhibit they produced lists that address, or the fraud check flags that exhibit.
3. MATERIALITY — element, defense, procedure/foundation, credibility on a
   contested issue, or damages. Reject incidental details.
4. TEXTUAL GROUNDING — every step must be in the two quotes. If you need
   unstated context, fail this step.
5. EVIDENCE, NOT ADVOCACY — fail the pair if the counter-quote is only another
   party's argument (a motion, proposed order, or attorney affirmation) rather
   than a sworn or documentary record.

contradiction_type must be one of: factual_contradiction, chronology_inconsistency,
material_omission, procedural_foundational, admission_against_interest.
Do not use date_inconsistency or logical_inconsistency.

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
    current_findings: list[Finding] | None = None,
) -> tuple[list[Finding], dict[str, Any]]:
    docs = cases_db.list_documents(case_id)
    snaps = {item["document_id"]: item for item in cases_db.list_case_snapshots(case_id)}
    _overlay_current_findings(snaps, document_id, text, current_findings)
    others = [doc for doc in docs if doc["id"] != document_id]
    is_filing = (doc_kind or "").lower() in COURT_FILING_KINDS

    if is_filing:
        claims = extract_claims(text, display_name, document_id, doc_kind=doc_kind)
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
                    doc_kind=doc.get("doc_kind"),
                    prefer_proof=True,
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
    internals = find_internal_evidence_defects(
        text=text,
        source=display_name,
        document_id=document_id,
        doc_kind=doc_kind,
    )
    tainted = propose_tainted_support(
        claims,
        docs,
        snaps,
        filing_text=text,
        focus_document_id=None if is_filing else document_id,
    )
    if not claims and not internals and not tainted:
        return [], empty
    if not evidence and not internals and not tainted:
        return [], empty

    candidates = propose_candidates(claims, evidence) if claims and evidence else []
    verified, rejected = verify_candidates(candidates, case_docs=docs)
    accepted = list(tainted)
    for item in internals:
        accepted.append(
            {
                **item,
                "contradiction_type": "chronology_inconsistency",
                "verification": _force_internal_pass(item),
            }
        )
    accepted.extend(verified)
    accepted.sort(key=lambda item: (0 if item.get("tainted_support") else 1))
    from app.quote_attest import gotcha_quotes_attest

    text_by_doc = {
        str(doc_id): str((snap or {}).get("text_excerpt") or "")
        for doc_id, snap in snaps.items()
    }
    if text:
        text_by_doc[str(document_id)] = text
    findings = [
        _to_finding(item, case_docs=docs)
        for item in accepted[:8]
        if _shippable(item, docs) and gotcha_quotes_attest(item, text_by_doc)
    ]
    timing = getattr(verify_candidates, "last_timing", None) or {}
    return findings, {
        "candidates": len(candidates) + len(internals),
        "accepted": len(findings),
        "rejected": rejected,
        **timing,
    }


def extract_claims(
    text: str,
    source: str,
    document_id: str,
    limit: int = 12,
    doc_kind: str | None = None,
    prefer_proof: bool = False,
) -> list[dict[str, Any]]:
    claims: list[dict[str, Any]] = []
    seen: set[str] = set()
    raw_limit = 40 if prefer_proof else limit
    for sentence in _sentences(text):
        if not CLAIM_CUE.search(sentence):
            continue
        key = sentence.lower()[:160]
        if key in seen:
            continue
        seen.add(key)
        claims.append(
            {
                "quote": sentence.strip()[:400],
                "source": source,
                "document_id": document_id,
                "doc_kind": doc_kind,
            }
        )
        if len(claims) >= raw_limit:
            break
    if prefer_proof:
        claims.sort(key=lambda item: (0 if _is_proof_oriented(item["quote"]) else 1))
    return claims[:limit]


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
        if classify_tier(doc, text, doc.get("display_name") or "", doc.get("doc_kind")) != TIER_EVIDENCE:
            continue
        snippets = _snippets(text, tokens)
        if not snippets:
            continue
        out.append(
            {
                "source": doc.get("display_name") or doc.get("original_filename"),
                "document_id": doc["id"],
                "doc_kind": doc.get("doc_kind"),
                "tier": TIER_EVIDENCE,
                "snippets": snippets[:3],
            }
        )
        if len(out) >= limit:
            break
    return out


def propose_tainted_support(
    claims: list[dict[str, Any]],
    docs: list[dict[str, Any]],
    snaps: dict[str, dict[str, Any]],
    filing_text: str = "",
    focus_document_id: str | None = None,
) -> list[dict[str, Any]]:
    """Link fraud-flagged exhibits to the claim they support when possible."""
    if focus_document_id:
        focus = next((doc for doc in docs if doc["id"] == focus_document_id), None)
        snap = snaps.get(focus_document_id) or {}
        rows = _standalone_tainted_rows(focus, snap) if focus else []
        if not (focus and rows):
            return []
        linked_claim = _claim_that_cites_exhibit(claims, focus, snaps, filing_text)
        return [_exhibit_edited_item(focus, rows, claim=linked_claim)]

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for claim in claims:
        for doc in _supporting_exhibit_docs(claim, filing_text, docs, snaps):
            if doc["id"] in seen:
                continue
            snap = snaps.get(doc["id"]) or {}
            rows = _tainted_rows(snap)
            if not _explicitly_uses_exhibit(claim, doc, snaps, filing_text):
                rows = [row for row in rows if str(row.get("analysis_type") or "") in INTERNAL_TAINTED]
            if not rows:
                continue
            seen.add(doc["id"])
            out.append(_exhibit_edited_item(doc, rows, claim=claim))
            if len(out) >= 6:
                return out
    return out


def _claim_that_cites_exhibit(
    claims: list[dict[str, Any]],
    doc: dict[str, Any],
    snaps: dict[str, dict[str, Any]],
    filing_text: str,
) -> dict[str, Any] | None:
    for claim in claims:
        if _explicitly_uses_exhibit(claim, doc, snaps, filing_text) or _claim_uses_exhibit(
            claim, doc, snaps, filing_text
        ):
            return claim
    return None


def _exhibit_edited_item(
    doc: dict[str, Any],
    rows: list[dict[str, Any]],
    claim: dict[str, Any] | None = None,
) -> dict[str, Any]:
    source = doc.get("display_name") or doc.get("original_filename") or "exhibit"
    detail, matter = _flagged_issue_detail(rows)
    claim_quote = str((claim or {}).get("quote") or "").strip()
    claim_source = str((claim or {}).get("source") or "").strip()
    if claim_quote:
        title = "Claim relies on exhibit that appears altered"
        matter = (
            "The claim uses this exhibit as support, but the exhibit itself carries "
            "scan-survivable fraud indicators. Credibility of that proof is at issue."
            if not matter
            else matter
        )
        verify = _force_tainted_pass(claim_quote, detail, bucket="credibility")
    else:
        title = "Exhibit appears fraudulently edited"
        verify = _force_tainted_pass("", detail, bucket="credibility")
    return {
        "title": title,
        "claim_quote": claim_quote,
        "claim_source": claim_source or source,
        "claim_document_id": (claim or {}).get("document_id") or doc.get("id"),
        "claim_kind": (claim or {}).get("doc_kind") or doc.get("doc_kind") or "exhibit",
        "evidence_quote": detail,
        "evidence_source": source,
        "document_id": doc["id"],
        "evidence_kind": doc.get("doc_kind") or "exhibit",
        "evidence_tier": TIER_EVIDENCE,
        "self_undercut": True,
        "tainted_support": True,
        "why_it_may_matter": matter,
        "contradiction_type": "procedural_foundational",
        "verification": verify,
    }


JURAT_OR_BARE_DATE = re.compile(
    r"(?i)^\s*(?:the\s+)?(?:\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+)?"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r"[,]?\s+\d{1,2},?\s+\d{4}\s*$"
    r"|^\s*\d{1,2}(?:st|nd|rd|th)?\s+day\s+of\s+"
    r"(?:january|february|march|april|may|june|july|august|september|october|november|december)"
    r",?\s+\d{4}\s*$"
)
FILING_NARRATIVE_KINDS = frozenset(
    {
        "motion",
        "complaint",
        "answer",
        "brief",
        "memorandum",
        "reply",
        "opposition",
        "letter",
        "notice",
        "affirmation",
        "affidavit",
        "filing",
        "summons",
    }
)


def keep_board_gotcha(
    item: dict[str, Any],
    docs: list[dict[str, Any]],
    snaps: dict[str, dict[str, Any]],
) -> bool:
    """Drop copied jurat dates and 'edited' cards that are not on a defective exhibit."""
    if item.get("tainted_support"):
        return _keep_tainted_board_card(item, docs, snaps)
    if _two_court_filings(item):
        return False
    claim = str(item.get("claim_quote") or "")
    evidence = str(item.get("evidence_quote") or "")
    if _ocr_broken(claim) or _ocr_broken(evidence):
        return False
    from app.quote_attest import gotcha_quotes_attest

    text_by_doc: dict[str, str] = {}
    for doc_id, snap in (snaps or {}).items():
        excerpt = str((snap or {}).get("text_excerpt") or "")
        if excerpt:
            text_by_doc[str(doc_id)] = excerpt
    if not gotcha_quotes_attest(item, text_by_doc):
        return False
    return True


def _keep_tainted_board_card(
    item: dict[str, Any],
    docs: list[dict[str, Any]],
    snaps: dict[str, dict[str, Any]],
) -> bool:
    doc_id = str(item.get("document_id") or "")
    doc = next((row for row in docs if row.get("id") == doc_id), None)
    kind = ((doc or {}).get("doc_kind") or item.get("evidence_kind") or "").lower()
    if kind in COURT_FILING_KINDS:
        return False
    snap = snaps.get(doc_id) or {}
    rows = _standalone_tainted_rows(doc or {"id": doc_id}, snap)
    if rows:
        detail, matter = _flagged_issue_detail(rows)
        claim_q = str(item.get("claim_quote") or "").strip()
        item["title"] = (
            "Claim relies on exhibit that appears altered"
            if claim_q
            else "Exhibit appears fraudulently edited"
        )
        # Keep linked claim quote when present; do not wipe it to empty.
        if not claim_q:
            item["claim_quote"] = ""
        item["evidence_quote"] = detail
        item["why_it_may_matter"] = matter
        item["gotcha_level"] = item["title"]
        if doc:
            item["display_name"] = doc.get("display_name") or doc.get("original_filename")
            item["related_documents"] = [
                {"document_id": doc_id, "display_name": item["display_name"]}
            ]
        return True
    quote = str(item.get("evidence_quote") or "").strip()
    if JURAT_OR_BARE_DATE.match(quote) or (
        "day of" in quote.lower() and "flagged" not in quote.lower() and len(quote) < 80
    ):
        return False
    return False


def _two_court_filings(item: dict[str, Any]) -> bool:
    claim_kind = str(item.get("claim_kind") or "").lower()
    evidence_kind = str(item.get("evidence_kind") or "").lower()
    if not claim_kind:
        claim_kind = _kind_from_source(str(item.get("claim_source") or ""))
    if not evidence_kind:
        evidence_kind = _kind_from_source(str(item.get("evidence_source") or ""))
    return claim_kind in FILING_NARRATIVE_KINDS and evidence_kind in FILING_NARRATIVE_KINDS


def _kind_from_source(name: str) -> str:
    blob = name.lower()
    if "exhibit" in blob:
        return "exhibit"
    if "affidavit" in blob or "affirm" in blob:
        return "affidavit"
    if "notice" in blob:
        return "notice"
    if "motion" in blob:
        return "motion"
    return ""


def _ocr_broken(text: str) -> bool:
    blob = text or ""
    if re.search(r"(?i)\b(tounsel|namemendoned|icater|plainitff)\b", blob):
        return True
    mixed = len(re.findall(r"[a-z][A-Z]", blob))
    return mixed >= 3


def _flagged_issue_detail(rows: list[dict[str, Any]]) -> tuple[str, str]:
    """Concrete issue text + why it matters, from the exhibit's forensic findings."""
    if not rows:
        return (
            "This exhibit appears fraudulently edited.",
            "Open the scan report and review each flagged item before treating the record as proof.",
        )

    primary = rows[0]
    title = str(primary.get("title") or "").strip()
    explanation = str(primary.get("explanation") or "").strip()
    why = str(primary.get("why_suspicious") or "").strip()
    quoted = str(primary.get("quoted_evidence") or "").strip()
    analysis = str(primary.get("analysis_type") or "").replace("_", " ").strip()

    detail = explanation or why or title or analysis
    detail = re.sub(r"(?i)^Automated document review reported:\s*", "", detail).strip()
    detail = re.sub(r"(?i)^Flagged on this exhibit:\s*", "", detail).strip()
    if quoted and quoted not in detail:
        detail = f"{detail.rstrip('.')} Quoted on the page: “{quoted}”." if detail else f"Quoted on the page: “{quoted}”."
    if not detail:
        detail = "Content on this exhibit appears altered."

    extras: list[str] = []
    for row in rows[1:3]:
        label = str(row.get("title") or "").strip()
        if not label:
            label = str(row.get("analysis_type") or "").replace("_", " ").strip()
        if label and label not in detail and label not in extras:
            extras.append(label)
    if extras:
        detail = detail.rstrip(".") + ". Also flagged: " + "; ".join(extras) + "."

    matter = why if why and why not in detail else (
        "A party's own exhibit that looks altered is not reliable proof until each flagged item is reviewed."
    )
    return detail, matter


def _flagged_item_summary(rows: list[dict[str, Any]]) -> str:
    detail, _ = _flagged_issue_detail(rows)
    return detail


def _supporting_exhibit_docs(
    claim: dict[str, Any],
    filing_text: str,
    docs: list[dict[str, Any]],
    snaps: dict[str, dict[str, Any]] | None = None,
    focus_document_id: str | None = None,
) -> list[dict[str, Any]]:
    snaps = snaps or {}
    if focus_document_id:
        focus = next((doc for doc in docs if doc["id"] == focus_document_id), None)
        if focus and _claim_uses_exhibit(claim, focus, snaps, filing_text):
            return [focus]
        return []
    matched: list[dict[str, Any]] = []
    for doc in docs:
        if doc.get("id") == claim.get("document_id"):
            continue
        if _claim_uses_exhibit(claim, doc, snaps, filing_text):
            matched.append(doc)
    return matched[:8]


def _claim_uses_exhibit(
    claim: dict[str, Any],
    doc: dict[str, Any],
    snaps: dict[str, dict[str, Any]],
    filing_text: str,
) -> bool:
    """True when the claim is using this record as proof, not merely naming the same street."""
    claim_quote = str(claim.get("quote") or "")
    claim_excerpt = str((snaps.get(claim.get("document_id") or "") or {}).get("text_excerpt") or filing_text)
    window = _window_around_claim(claim_excerpt, claim_quote)
    cited = _exhibit_tags(claim_quote) | _exhibit_tags(window)
    tag = _doc_exhibit_tag(doc)
    return _cited_exhibit_match(cited, tag)


def _explicitly_uses_exhibit(
    claim: dict[str, Any],
    doc: dict[str, Any],
    snaps: dict[str, dict[str, Any]],
    filing_text: str,
) -> bool:
    claim_quote = str(claim.get("quote") or "")
    claim_excerpt = str((snaps.get(claim.get("document_id") or "") or {}).get("text_excerpt") or filing_text)
    window = _window_around_claim(claim_excerpt, claim_quote)
    cited = _exhibit_tags(claim_quote) | _exhibit_tags(window)
    return _cited_exhibit_match(cited, _doc_exhibit_tag(doc))


def _cited_exhibit_match(cited: set[str], tag: str | None) -> bool:
    """Only a named exhibit tag binds. 'In support' on an affidavit is not a cite."""
    if not cited or not tag:
        return False
    return tag in cited or any(_tags_compatible(tag, other) for other in cited)


def _is_proof_oriented(text: str) -> bool:
    return bool(
        _denies_residence(text)
        or _asserts_residence(text)
        or PRODUCED_CUE.search(text or "")
        or EXHIBIT_TAG.search(text or "")
        or BILL_OR_STATEMENT.search(text or "")
    )


def _standalone_tainted_rows(doc: dict[str, Any], snap: dict[str, Any]) -> list[dict[str, Any]]:
    del doc
    return [
        row
        for row in _tainted_rows(snap)
        if str(row.get("analysis_type") or "") in INTERNAL_TAINTED
    ]


def _overlay_current_findings(
    snaps: dict[str, dict[str, Any]],
    document_id: str,
    text: str,
    current_findings: list[Finding] | None,
) -> None:
    if not document_id:
        return
    snap = dict(snaps.get(document_id) or {})
    rows = list(snap.get("findings_summary") or [])
    for item in current_findings or []:
        ev = item.evidence or {}
        rows.append(
            {
                "id": item.id,
                "category": item.category,
                "severity": item.severity,
                "title": item.title,
                "analysis_type": ev.get("analysis_type"),
                "quoted_evidence": ev.get("quoted_evidence") or ev.get("evidence_quote"),
                "why_suspicious": item.why_suspicious,
                "explanation": item.explanation,
            }
        )
    if text and not snap.get("text_excerpt"):
        snap["text_excerpt"] = text[:24000]
    elif text:
        snap["text_excerpt"] = snap.get("text_excerpt") or text[:24000]
    snap["findings_summary"] = rows
    snaps[document_id] = snap


def _window_around_claim(text: str, quote: str, radius: int = 400) -> str:
    hay = text or ""
    needle = (quote or "")[:80]
    at = hay.lower().find(needle.lower()) if needle else -1
    if at < 0:
        return quote
    return hay[max(0, at - radius) : at + len(quote) + radius]


def _exhibit_tags(text: str) -> set[str]:
    tags: set[str] = set()
    for match in EXHIBIT_TAG.finditer(text or ""):
        letter = match.group(1).upper()
        number = match.group(2)
        tags.add(f"{letter}_{number}" if number else letter)
    return tags


def _doc_exhibit_tag(doc: dict[str, Any]) -> str | None:
    tags = _exhibit_tags(f"{doc.get('display_name') or ''} {doc.get('original_filename') or ''}")
    return next(iter(tags), None)


def _tags_compatible(left: str, right: str) -> bool:
    if left == right:
        return True
    return left.split("_")[0] == right.split("_")[0] and (
        "_" in left and "_" in right and left.split("_")[1] == right.split("_")[1]
    )


def _tainted_rows(snap: dict[str, Any]) -> list[dict[str, Any]]:
    """Content defects that survive a paper scan. Bare ELA / metadata alone do not count."""
    out: list[dict[str, Any]] = []
    for row in snap.get("findings_summary") or []:
        if not isinstance(row, dict):
            continue
        if row.get("gotcha") or row.get("category") in {"gotcha", "case_consistency"}:
            continue
        if str(row.get("severity") or "") not in {"medium", "high", "critical"}:
            continue
        analysis = str(row.get("analysis_type") or "")
        if analysis in INTERNAL_TAINTED:
            out.append(row)
            continue
        if analysis in {"text_overlay", "graphic_manipulation"} and not row.get("scan_heavy"):
            out.append(row)
    return out[:3]


def _force_tainted_pass(
    claim: str,
    evidence: str,
    *,
    bucket: str = "credibility",
) -> dict[str, Any]:
    linked = bool(str(claim or "").strip())
    return {
        "same_fact": {
            "pass": True,
            "fact_named": (
                "authenticity of the exhibit offered to support the claim"
                if linked
                else "authenticity of the offered exhibit"
            ),
            "reason": (
                "The claim relies on an exhibit that itself appears altered."
                if linked
                else "The exhibit itself is the record being checked."
            ),
        },
        "directionality": {
            "pass": True,
            "evidence_asserts": evidence[:220],
            "runs": "against",
            "reason": "The exhibit appears fraudulently edited.",
        },
        "materiality": {
            "pass": True,
            "tier": bucket,
            "reason": (
                f"The contradicted fact bears on {bucket}."
                if linked
                else "Open the report and review each flagged item before treating this exhibit as proof."
            ),
        },
        "textual_grounding": {"pass": True, "reason": "The flagged items are on the exhibit scan report."},
        "contradiction_type": "procedural_foundational",
        "pass": True,
        "failed_step": None,
        "gate": "tainted_support",
    }


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


def verify_candidates(
    candidates: list[dict[str, Any]],
    case_docs: list[dict[str, Any]] | None = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Local gates first; model verify only for ambiguous pairs (cuts Claude latency)."""
    import time

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    ambiguous: list[tuple[int, dict[str, Any], dict[str, Any]]] = []

    for idx, candidate in enumerate(candidates):
        if not evidence_is_probative(candidate, case_docs):
            rejected.append(
                _reject_record(
                    candidate,
                    {
                        "pass": False,
                        "failed_step": "evidence_tier",
                        "gate": "advocacy_not_evidence",
                    },
                )
            )
            continue
        local = verify_pair(candidate)
        veto = hard_veto_step(candidate, local)
        if veto:
            rejected.append(
                _reject_record(
                    candidate,
                    {
                        **local,
                        "pass": False,
                        "failed_step": veto,
                        "gate": "hard_veto",
                    },
                )
            )
            continue
        if _complete_pass(local):
            verdict = merge_verdicts(candidate, local, None)
            record = {**candidate, "verification": verdict}
            if verdict.get("pass") and _complete_pass(verdict):
                record["contradiction_type"] = _canonical_type(
                    verdict.get("contradiction_type"),
                    local,
                    allow_retire_remap=verdict.get("gate") == "local",
                )
                if record["contradiction_type"] is None:
                    rejected.append(
                        _reject_record(
                            candidate,
                            {
                                **verdict,
                                "pass": False,
                                "failed_step": "contradiction_type",
                                "gate": "retired_type",
                            },
                        )
                    )
                else:
                    accepted.append(record)
            else:
                rejected.append(_reject_record(candidate, verdict))
            continue
        ambiguous.append((idx, candidate, local))

    model_results: dict[int, dict[str, Any]] = {}
    verify_ms = 0
    if ambiguous and (
        settings.gemini_api_key or settings.anthropic_api_key or settings.openai_api_key
    ):
        batch = [row[1] for row in ambiguous]
        t0 = time.perf_counter()
        raw = _ask_verify(batch)
        verify_ms = int((time.perf_counter() - t0) * 1000)
        # Remap batch indices 0..n back to original candidate indices.
        for batch_i, (orig_idx, _cand, _local) in enumerate(ambiguous):
            if batch_i in raw:
                model_results[orig_idx] = raw[batch_i]

    for orig_idx, candidate, local in ambiguous:
        verdict = merge_verdicts(candidate, local, model_results.get(orig_idx))
        record = {**candidate, "verification": verdict}
        if verdict.get("pass") and _complete_pass(verdict):
            record["contradiction_type"] = _canonical_type(
                verdict.get("contradiction_type"),
                local,
                allow_retire_remap=verdict.get("gate") == "local",
            )
            if record["contradiction_type"] is None:
                rejected.append(
                    _reject_record(
                        candidate,
                        {
                            **verdict,
                            "pass": False,
                            "failed_step": "contradiction_type",
                            "gate": "retired_type",
                        },
                    )
                )
            else:
                accepted.append(record)
        else:
            rejected.append(_reject_record(candidate, verdict))

    # Stash timing on rejected sentinel for callers that inspect logs — find_gotchas reads via return.
    verify_candidates.last_timing = {  # type: ignore[attr-defined]
        "gotcha_verify_ms": verify_ms,
        "verify_candidate_count": len(candidates),
        "verify_ambiguous_count": len(ambiguous),
        "verify_provider": _verify_provider_label(),
    }
    return accepted, rejected


def _verify_provider_label() -> str:
    try:
        from app.llm_verify import resolve_verify_provider

        return resolve_verify_provider()
    except Exception:
        return "unknown"


def merge_verdicts(
    candidate: dict[str, Any],
    local: dict[str, Any],
    model: dict[str, Any] | None,
) -> dict[str, Any]:
    """One gate. Model pass without four completed steps does not ship."""
    veto = hard_veto_step(candidate, local)
    if veto:
        out = dict(local)
        out["pass"] = False
        out["failed_step"] = veto
        out["gate"] = "hard_veto"
        return out
    if _complete_pass(local):
        out = dict(local)
        out["gate"] = "local"
        return _require_case_impact(candidate, out)
    model_type = _canonical_type(model.get("contradiction_type") if model else None, allow_retire_remap=False)
    if model and _complete_pass(model) and model_type:
        out = dict(model)
        out["contradiction_type"] = model_type
        out["gate"] = "model_complete"
        return _require_case_impact(candidate, out)
    if model and not model.get("pass"):
        out = dict(model)
        out["pass"] = False
        out["failed_step"] = out.get("failed_step") or _first_fail(out)
        out["gate"] = "model_reject"
        return out
    out = dict(local)
    out["pass"] = False
    out["failed_step"] = out.get("failed_step") or _first_fail(out)
    out["gate"] = "suppressed_ambiguous"
    return out


def _require_case_impact(candidate: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    """Ambiguous pairs that cannot name a case-impact bucket do not ship."""
    if candidate.get("tainted_support"):
        return verdict
    claim = str(candidate.get("claim_quote") or "")
    evidence = str(candidate.get("evidence_quote") or "")
    fact = str((verdict.get("same_fact") or {}).get("fact_named") or "")
    bucket = _case_impact_bucket(claim, evidence, fact)
    if bucket:
        material = dict(verdict.get("materiality") or {})
        material["pass"] = True
        material["tier"] = bucket
        material["reason"] = f"The contradicted fact bears on {bucket}."
        out = dict(verdict)
        out["materiality"] = material
        return out
    out = dict(verdict)
    out["pass"] = False
    out["failed_step"] = "materiality"
    out["gate"] = "no_case_impact"
    material = dict(out.get("materiality") or {})
    material["pass"] = False
    material["tier"] = "incidental"
    material["reason"] = (
        "Cannot name an element, procedure, credibility, or damages impact — suppress."
    )
    out["materiality"] = material
    return out


def hard_veto_step(candidate: dict[str, Any], local: dict[str, Any]) -> str | None:
    """Deterministic rejects that a model cannot override."""
    claim = str(candidate.get("claim_quote") or "")
    evidence = str(candidate.get("evidence_quote") or "")
    if not _usable(candidate):
        return "textual_grounding"
    fact_named = str((local.get("same_fact") or {}).get("fact_named") or "")
    if _boilerplate_collision(claim, evidence, fact_named):
        return "same_fact"
    if _duty_vs_place(claim, evidence):
        return "same_fact"
    if _caption_or_form(evidence) or _party_list_only(claim, evidence):
        return "same_fact"
    if _characterization_vs_docket(claim, evidence):
        return "same_fact"
    if _appearance_vs_service(claim, evidence) or _appearance_vs_identity(claim, evidence):
        return "same_fact"
    if _settlement_topic_only(claim, evidence):
        return "same_fact"
    if _service_vs_address_listing(claim, evidence):
        return "same_fact"
    if PROCESS_DEFECT.search(evidence) and _is_denial(claim):
        return "directionality"
    if _ordinary_traverse(claim, evidence) or _process_form_variant(claim, evidence):
        return "directionality"
    if _corroborates(claim, evidence):
        return "directionality"
    if _date_format_only(claim, evidence) or _incidental(claim, evidence):
        return "materiality"
    if _two_court_filings(candidate):
        return "same_fact"
    if _ocr_broken(claim) or _ocr_broken(evidence):
        return "textual_grounding"
    if local.get("failed_step") == "textual_grounding":
        return "textual_grounding"
    if (local.get("directionality") or {}).get("pass") and not _case_impact_bucket(
        claim, evidence, fact_named
    ):
        return "materiality"
    return None


def verify_pair(candidate: dict[str, Any]) -> dict[str, Any]:
    """Local four-part check. Conservative: when unsure, fail the pair."""
    claim = str(candidate.get("claim_quote") or "")
    evidence = str(candidate.get("evidence_quote") or "")
    overlap = _stem_overlap(claim, evidence)
    # Drop boilerplate stems so "United States" / "made" never look like a shared fact.
    meaningful = {s for s in overlap if s not in BOILERPLATE_STEMS}

    same = {
        "pass": False,
        "fact_named": "",
        "reason": "No shared nameable fact in the two quotes.",
    }
    if _boilerplate_collision(claim, evidence, ", ".join(sorted(overlap)[:6])):
        same["reason"] = (
            "Shared words are legal or English boilerplate (e.g. United States, made, person), "
            "not a nameable disputed fact."
        )
    elif _duty_vs_place(claim, evidence):
        same["reason"] = "Claim is about a duty or condition; evidence is about place. Different facts."
    elif _caption_or_form(evidence) or _party_list_only(claim, evidence):
        same["reason"] = "Shared party names, a caption, or a form checkbox are not a nameable fact."
    elif _characterization_vs_docket(claim, evidence):
        same["reason"] = (
            "A party's characterization of whether they had a chance to participate "
            "is not the same fact as a calendar line that a conference was listed."
        )
    elif _appearance_vs_service(claim, evidence) or _appearance_vs_identity(claim, evidence):
        same["reason"] = (
            "Whether a party answered is not the same fact as identity, counsel, "
            "or whether papers were mailed or served."
        )
    elif _settlement_topic_only(claim, evidence):
        same["reason"] = "A 'not settled' claim is not the same fact as a document that merely names settlement."
    elif _service_vs_address_listing(claim, evidence):
        same["reason"] = (
            "Whether process was attempted is not the same fact as an address or identity listing."
        )
    elif _denies_residence(claim) and _same_street_number(claim, evidence) and not _denies_residence(evidence):
        same = {
            "pass": True,
            "fact_named": "residence at the stated address",
            "reason": "The claim denies living at an address that the record lists.",
        }
    elif (
        PLACE_FACT.search(claim)
        and PLACE_FACT.search(evidence)
        and _shared_calendar_day(claim, evidence)
        and _different_places(claim, evidence)
    ):
        same = {
            "pass": True,
            "fact_named": "whereabouts on a shared date",
            "reason": "Both quotes address where a person was on the same date.",
        }
    elif len(meaningful) >= 2 and _has_opposing_polarity(claim, evidence) and (
        _predicate_overlap(claim, evidence) or _exclusive_vs_instance(claim, evidence)
    ):
        same = {
            "pass": True,
            "fact_named": ", ".join(sorted(meaningful)[:6]),
            "reason": "Both quotes share the same discrete fact and opposing polarity.",
        }
    elif len(meaningful) == 1 and (
        (_has_opposing_polarity(claim, evidence) and _predicate_overlap(claim, evidence))
        or _exclusive_vs_instance(claim, evidence)
    ):
        same = {
            "pass": True,
            "fact_named": next(iter(meaningful)),
            "reason": "One shared fact token plus opposing polarity on the same predicate.",
        }

    evidence_asserts = evidence[:220]
    runs = "neither"
    direction_pass = False
    direction_reason = "Evidence does not clearly run against the claim."
    if _ordinary_traverse(claim, evidence) or _process_form_variant(claim, evidence):
        runs = "neither"
        direction_reason = (
            "A denial of service or a process-server form variant versus the other side's "
            "completed-service record is the ordinary traverse, not a gotcha."
        )
    elif _corroborates(claim, evidence):
        runs = "for"
        direction_reason = "The evidence quote restates or supports the claim on the same fact."
    elif same["pass"] and PROCESS_DEFECT.search(evidence) and _is_denial(claim):
        runs = "for"
        direction_reason = (
            "Evidence describes a defect or failure about the same subject, which supports "
            "the denial rather than contradicting it."
        )
    elif same["pass"] and same.get("fact_named") == "whereabouts on a shared date":
        runs = "against"
        direction_pass = True
        direction_reason = "The evidence appears to place the person somewhere else on the same date."
    elif same["pass"] and same.get("fact_named") == "residence at the stated address":
        runs = "against"
        direction_pass = True
        direction_reason = "The record lists the address the claimant says they did not live at."
    elif same["pass"] and _has_opposing_polarity(claim, evidence):
        runs = "against"
        direction_pass = True
        direction_reason = "The evidence quote appears to assert the opposite of the claim on the same fact."
    direction = {
        "pass": direction_pass,
        "evidence_asserts": evidence_asserts,
        "runs": runs,
        "reason": direction_reason,
    }

    incidental = _incidental(claim, evidence) or _date_format_only(claim, evidence)
    impact = _case_impact_bucket(claim, evidence, str(same.get("fact_named") or ""))
    material_pass = bool(direction_pass and not incidental and impact)
    material = {
        "pass": material_pass,
        "tier": impact or "incidental",
        "reason": (
            f"The contradicted fact bears on {impact}."
            if material_pass and impact
            else "Cannot name an element, procedure, credibility, or damages impact — suppress."
            if direction_pass and not incidental
            else "The difference looks incidental, formatting-level, or the same date written two ways."
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
    return {
        "same_fact": same,
        "directionality": direction,
        "materiality": material,
        "textual_grounding": textual,
        "contradiction_type": _local_type(claim),
        "pass": failed is None,
        "failed_step": failed,
        "gate": "local",
    }


def _token_candidates(claims: list[dict[str, Any]], evidence: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out: list[dict[str, Any]] = []
    for claim in claims:
        quote = claim["quote"]
        toks = {s for s in set(_stems(_tokens(quote))) if s not in BOILERPLATE_STEMS}
        if not toks:
            continue
        for item in evidence:
            if item.get("document_id") == claim.get("document_id"):
                continue
            for snippet in item.get("snippets") or []:
                overlap = toks & {s for s in set(_stems(_tokens(snippet))) if s not in BOILERPLATE_STEMS}
                if (
                    len(overlap) < 2
                    and not _has_opposing_polarity(quote, snippet)
                    and not (len(overlap) >= 1 and _exclusive_vs_instance(quote, snippet))
                ):
                    continue
                if _boilerplate_collision(quote, snippet, ", ".join(sorted(overlap)[:6])):
                    continue
                out.append(
                    {
                        "title": "Possible claim/evidence conflict",
                        "claim_quote": quote,
                        "claim_source": claim["source"],
                        "claim_document_id": claim.get("document_id"),
                        "claim_kind": claim.get("doc_kind"),
                        "evidence_quote": snippet[:320],
                        "evidence_source": item["source"],
                        "document_id": item.get("document_id"),
                        "evidence_kind": item.get("doc_kind"),
                        "evidence_tier": item.get("tier") or TIER_EVIDENCE,
                        "why_it_may_matter": "",
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
    from app.llm_verify import ask_verify

    return ask_verify(
        candidates,
        verify_prompt=VERIFY_PROMPT,
        verify_schema=VERIFY_SCHEMA,
        first_fail=_first_fail,
    )

def _to_finding(item: dict[str, Any], case_docs: list[dict[str, Any]] | None = None) -> Finding:
    vtype = _canonical_type(item.get("contradiction_type"), item.get("verification") or {}) or "factual_contradiction"
    verify = item.get("verification") or {}
    seed = f"gotcha|{item.get('claim_quote')}|{item.get('evidence_quote')}"
    fid = "ctx-gotcha-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
    ev_tier = TIER_EVIDENCE if evidence_is_probative(item, case_docs) else TIER_ADVOCACY
    c_tier = claim_tier(item, case_docs)
    self_hit = bool(item.get("self_undercut") or is_self_undercut(item))
    tainted_hit = bool(item.get("tainted_support"))
    if tainted_hit:
        severity = "high"
        confidence = 90.0
        title = str(item.get("title") or "Exhibit appears fraudulently edited")
        self_hit = True
    elif self_hit:
        severity = "high"
        confidence = 88.0
        title = "Apparent self-contradiction"
    elif ev_tier == TIER_EVIDENCE and c_tier == TIER_EVIDENCE:
        severity = "high"
        confidence = 82.0
        title = TYPE_LABELS[vtype]
    else:
        severity = "medium"
        confidence = 70.0
        title = TYPE_LABELS[vtype]
    if vtype in {"procedural_foundational", "admission_against_interest"} and severity != "high":
        severity = "high"
        confidence = max(confidence, 82.0)
    fact_named = str((verify.get("same_fact") or {}).get("fact_named") or "").strip()
    direction_reason = str((verify.get("directionality") or {}).get("reason") or "").strip()
    material_reason = str((verify.get("materiality") or {}).get("reason") or "").strip()
    attorney_why = (
        str(item.get("why_it_may_matter") or "").strip()
        if tainted_hit
        else _attorney_why(fact_named, direction_reason, material_reason, item.get("why_it_may_matter"))
    )
    ev_label = counter_label(ev_tier)
    if tainted_hit:
        detail = str(item.get("evidence_quote") or "").strip()
        claim_q = str(item.get("claim_quote") or "").strip()
        if claim_q:
            explanation = (
                f"CLAIM ({item.get('claim_source') or 'filing'}): “{claim_q}” "
                f"EXHIBIT ({item.get('evidence_source') or 'record'}): {detail}"
            )
        else:
            explanation = detail or attorney_why or (
                "This exhibit appears fraudulently edited. See the report for each flagged item."
            )
        seed = f"gotcha|edited|{item.get('document_id')}|{item.get('evidence_source')}|{claim_q[:40]}"
        fid = "ctx-gotcha-" + hashlib.sha1(seed.encode("utf-8")).hexdigest()[:10]
        why_suspicious = attorney_why if attorney_why and attorney_why not in explanation else (
            "Open the scan report and review each flagged item before treating this exhibit as proof."
        )
    else:
        explanation = (
            f"CLAIM ({item.get('claim_source') or 'filing'}): “{item.get('claim_quote')}” "
            f"{ev_label.upper()} ({item.get('evidence_source') or 'record'}): “{item.get('evidence_quote')}”"
        )
        why_suspicious = attorney_why or direction_reason
    return finding(
        fid=fid,
        category="gotcha",
        severity=severity,  # type: ignore[arg-type]
        title=title,
        explanation=explanation,
        why_suspicious=why_suspicious,
        confidence=confidence,
        evidence={
            "gotcha": True,
            "verified": True,
            "gotcha_level": title,
            "contradiction_type": vtype,
            "analysis_type": "gotcha",
            "claim_quote": item.get("claim_quote") or None,
            "claim_source": item.get("claim_source") or None,
            "evidence_quote": item.get("evidence_quote"),
            "evidence_source": item.get("evidence_source"),
            "evidence_label": ev_label,
            "evidence_tier": ev_tier,
            "claim_tier": c_tier,
            "self_undercut": self_hit,
            "tainted_support": tainted_hit,
            "document_id": item.get("document_id"),
            "fact_named": None if (tainted_hit and not item.get("claim_quote")) else fact_named,
            "direction_reason": None if (tainted_hit and not item.get("claim_quote")) else direction_reason,
            "materiality_reason": None if (tainted_hit and not item.get("claim_quote")) else material_reason,
            "attorney_why": attorney_why,
            "verification": verify,
            "material": True,
            "prior_display_name": item.get("evidence_source"),
            "case_impact": "case_winning" if tainted_hit else None,
            "case_impact_bucket": "credibility" if tainted_hit else None,
        },
        recommended_action=(
            "Open the scan report and review each flagged item before treating this exhibit as proof."
            if tainted_hit
            else (
                f"Open “{item.get('claim_source')}” and “{item.get('evidence_source')}” and confirm "
                "the quoted language before relying on this as impeachment or in a filing."
            )
        ),
    )


def _attorney_why(
    fact_named: str,
    direction_reason: str,
    material_reason: str,
    extra: Any,
) -> str:
    parts = []
    if fact_named:
        parts.append(f"Same fact that appears to be in play: {fact_named}.")
    if direction_reason:
        parts.append(direction_reason.rstrip(".") + ".")
    if material_reason:
        parts.append(material_reason.rstrip(".") + ".")
    extra_text = str(extra or "").strip()
    if extra_text and extra_text.lower() not in " ".join(parts).lower():
        parts.append(extra_text)
    return " ".join(parts)


def _shippable(item: dict[str, Any], case_docs: list[dict[str, Any]] | None = None) -> bool:
    verify = item.get("verification") or {}
    if not verify.get("pass") or not _complete_pass(verify):
        return False
    if item.get("tainted_support"):
        if not item.get("evidence_quote"):
            return False
    elif not (item.get("claim_quote") and item.get("evidence_quote")):
        return False
    if not evidence_is_probative(item, case_docs):
        return False
    return _canonical_type(item.get("contradiction_type"), verify) in CONTRADICTION_TYPES


def _force_internal_pass(item: dict[str, Any]) -> dict[str, Any]:
    return {
        "same_fact": {"pass": True, "fact_named": "stated weekday versus calendar date", "reason": "Same date is named two ways."},
        "directionality": {
            "pass": True,
            "evidence_asserts": item.get("evidence_quote"),
            "runs": "against",
            "reason": "The calendar does not match the weekday written in the record.",
        },
        "materiality": {
            "pass": True,
            "tier": "foundation",
            "reason": "A sworn or documentary date that names the wrong weekday is foundational.",
        },
        "textual_grounding": {"pass": True, "reason": "The date and weekday are in the pulled sentence."},
        "contradiction_type": "chronology_inconsistency",
        "pass": True,
        "failed_step": None,
        "gate": "internal_tier1",
    }


def _reject_record(candidate: dict[str, Any], verdict: dict[str, Any]) -> dict[str, Any]:
    return {
        "title": candidate.get("title"),
        "claim_quote": candidate.get("claim_quote"),
        "evidence_quote": candidate.get("evidence_quote"),
        "failed_step": verdict.get("failed_step") or _first_fail(verdict),
        "gate": verdict.get("gate"),
        "verification": verdict,
    }


def _complete_pass(verdict: dict[str, Any] | None) -> bool:
    if not verdict or not verdict.get("pass"):
        return False
    for step in VERIFY_STEPS:
        block = verdict.get(step)
        if not isinstance(block, dict) or block.get("pass") is not True:
            return False
    runs = str((verdict.get("directionality") or {}).get("runs") or "").lower()
    if runs and runs != "against":
        return False
    return True


def _usable(item: dict[str, Any]) -> bool:
    claim = str(item.get("claim_quote") or "").strip()
    evidence = str(item.get("evidence_quote") or "").strip()
    return len(claim) >= 12 and len(evidence) >= 12 and claim.lower() != evidence.lower()


def _tokens(text: str) -> list[str]:
    words = re.findall(r"[a-z0-9']{4,}", (text or "").lower())
    return [w for w in words if w not in STOP]


def _stems(tokens: list[str] | set[str]) -> list[str]:
    out: list[str] = []
    for token in tokens:
        stem = token[:-1] if token.endswith("s") and len(token) > 4 else token
        out.append(stem)
    return out


def _stem_overlap(claim: str, evidence: str) -> set[str]:
    return set(_stems(_tokens(claim))) & set(_stems(_tokens(evidence)))


def _snippets(text: str, tokens: list[str], window: int = 280, limit: int = 3) -> list[str]:
    if not text or not tokens:
        return []
    hay = text.lower()
    stems = _stems(tokens)
    spans: list[tuple[int, int]] = []
    for token in list(tokens[:12]) + stems[:12]:
        at = hay.find(token)
        if at < 0:
            continue
        spans.append((max(0, at - window // 3), min(len(text), at + window)))
    out = []
    seen: set[str] = set()
    for left, right in spans:
        chunk = re.sub(r"\s+", " ", text[left:right]).strip()
        key = chunk[:80]
        if chunk and key not in seen:
            seen.add(key)
            out.append(chunk)
        if len(out) >= limit:
            break
    return out


def _sentences(text: str) -> list[str]:
    parts = re.split(r"(?<=[.!?])\s+", text or "")
    return [part.strip() for part in parts if 24 <= len(part.strip()) <= 500]


def _is_denial(text: str) -> bool:
    return bool(
        re.search(
            r"(?i)\b(never|did\s+not|do\s+not|was\s+not|were\s+not|without|"
            r"no\s+(?:knowledge|prior|other|income|contact|injury|complaints?))\b",
            text,
        )
    )


def _shared_year(left: str, right: str) -> bool:
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", left)) & set(re.findall(r"\b(?:19|20)\d{2}\b", right))
    return bool(years)


def _shared_calendar_day(left: str, right: str) -> bool:
    """Same month-day-year, not merely the same index year on every caption."""
    return bool(_day_keys(left) & _day_keys(right))


def _day_keys(text: str) -> set[str]:
    keys: set[str] = set()
    for match in re.finditer(r"\b(\d{1,2})/(\d{1,2})/(\d{2,4})\b", text or ""):
        month, day, year = int(match.group(1)), int(match.group(2)), int(match.group(3))
        if year < 100:
            year += 2000 if year <= 39 else 1900
        keys.add(f"{year:04d}-{month:02d}-{day:02d}")
    for match in re.finditer(
        r"\b(January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+(\d{1,2}),?\s+(\d{4})\b",
        text or "",
        re.I,
    ):
        month = {
            "january": 1,
            "february": 2,
            "march": 3,
            "april": 4,
            "may": 5,
            "june": 6,
            "july": 7,
            "august": 8,
            "september": 9,
            "october": 10,
            "november": 11,
            "december": 12,
        }[match.group(1).lower()]
        keys.add(f"{int(match.group(3)):04d}-{month:02d}-{int(match.group(2)):02d}")
    return keys


def _duty_vs_place(claim: str, evidence: str) -> bool:
    if DUTY_OR_CONDITION.search(claim) and PLACE_FACT.search(evidence) and not DUTY_OR_CONDITION.search(evidence):
        return True
    if PLACE_FACT.search(claim) and DUTY_OR_CONDITION.search(evidence) and not PLACE_FACT.search(evidence):
        return True
    return False


def _exclusive_vs_instance(claim: str, evidence: str) -> bool:
    return bool(EXCLUSIVE_OR_COMPLETE.search(claim) and COUNTER_INSTANCE.search(evidence))


def _predicate_overlap(claim: str, evidence: str) -> bool:
    shared = _stem_overlap(claim, evidence)
    # Legal-process nouns (notice, motion, complaint, settlement) are topics,
    # not a nameable fact. Keep markers that name a discrete act or status.
    markers = {
        "signed",
        "signature",
        "income",
        "injury",
        "contact",
        "collection",
        "agreement",
        "warning",
        "theft",
        "store",
        "stipulation",
        "knowledge",
        "delivered",
        "authorized",
        "disclosed",
        "account",
        "complaints",
    }
    return bool(shared & markers)


def _has_opposing_polarity(claim: str, evidence: str) -> bool:
    if PROCESS_DEFECT.search(evidence) and _is_denial(claim):
        return False
    if _corroborates(claim, evidence) or _ordinary_traverse(claim, evidence):
        return False
    if _is_denial(claim) and not _is_denial(evidence):
        return True
    if not _is_denial(claim) and _is_denial(evidence):
        return True
    if _exclusive_vs_instance(claim, evidence):
        return True
    if re.search(r"(?i)\b(after|despite|continued|kept)\b", claim) and re.search(
        r"(?i)\b(agree|stipulat|settlement|cancel|discharge|order)\b", evidence
    ):
        return True
    return False


def _case_impact_bucket(claim: str, evidence: str, fact_named: str = "") -> str | None:
    """Name element / procedure / credibility / damages, or None to suppress."""
    if _boilerplate_collision(claim, evidence, fact_named):
        return None
    blob = f"{claim} {evidence} {fact_named}".lower()
    fact = (fact_named or "").lower()
    if fact in {
        "residence at the stated address",
        "whereabouts on a shared date",
        "authenticity of the offered exhibit",
        "authenticity of the exhibit offered to support the claim",
    }:
        return "credibility" if "authenticity" in fact else "element"
    try:
        from app.practice_packs import pack_impact_cues

        for cue in pack_impact_cues():
            if cue and cue in blob:
                if any(k in cue for k in ("fee", "damage", "amount", "eob", "ucr")):
                    return "damages"
                if any(k in cue for k in ("discharge", "crfn", "foundation", "chart", "consent")):
                    return "procedure"
                return "element"
    except Exception:
        pass
    if _denies_residence(claim) or _asserts_residence(claim):
        if SERVICE_FACT.search(blob) or PLACE_FACT.search(blob):
            return "element"
    if SERVICE_FACT.search(claim) and (
        SERVICE_FACT.search(evidence) or PLACE_FACT.search(evidence) or _denies_residence(evidence)
    ):
        # Competing service recitals are ordinary traverse — not an element interrupt.
        if _ordinary_traverse(claim, evidence):
            return None
        return "element"
    if re.search(
        r"(?i)\b("
        r"jurisdiction|standing|default|summons|complaint|"
        r"personal\s+jurisdiction|suitable\s+age|conspicuous|"
        r"affidavit\s+of\s+service|process\s+server"
        r")\b",
        blob,
    ):
        if _ordinary_traverse(claim, evidence) or _boilerplate_collision(claim, evidence, fact_named):
            return None
        return "element"
    if re.search(
        r"(?i)\b("
        r"discharge|satisfaction\s+of\s+mortgage|crfn|"
        r"authority|authorized\s+to\s+(?:act|represent)|"
        r"not\s+represented|foundation|so-?ordered|entered"
        r")\b",
        blob,
    ):
        return "procedure"
    if re.search(
        r"(?i)\b("
        r"fraud|forged|fabricat|alter(?:ed|ation)|tainted|"
        r"false\s+(?:statement|exhibit|record)|under\s+oath|"
        r"perjur|credibility|impeach"
        r")\b",
        blob,
    ):
        return "credibility"
    if re.search(
        r"(?i)\b("
        r"damages|amount\s+due|fees?|attorney'?s?\s+fees|"
        r"accrued\s+interest|late\s+charge|balance\s+due"
        r")\b",
        blob,
    ):
        return "damages"
    if re.search(
        r"(?i)\b("
        r"never|did\s+not|was\s+not|exclusive|sole|disclosed\s+all|"
        r"no\s+prior|no\s+other|no\s+knowledge|not\s+aware|first\s+learned|"
        r"under\s+penalty|every\s+weekday|primary\s+caregiver"
        r")\b",
        claim,
    ) and _has_opposing_polarity(claim, evidence):
        return "credibility"
    if re.search(
        r"(?i)\b("
        r"caregiver|custody|parenting|visitation|net\s+worth|"
        r"change-?order|no\s+prior\s+complaints?"
        r")\b",
        blob,
    ):
        return "element" if re.search(r"(?i)\b(caregiver|custody|parenting|visitation)\b", blob) else "credibility"
    if re.search(
        r"(?i)\b("
        r"after\s+agreeing|stipulation\s+of\s+settlement|"
        r"continued\s+to\s+(?:send|collect|demand)|"
        r"cancels?\s+collection|discharges?\s+it"
        r")\b",
        blob,
    ):
        return "procedure"
    return None


def _boilerplate_collision(claim: str, evidence: str, fact_named: str = "") -> bool:
    """True when the only 'shared fact' is legal/English boilerplate (Mamiye AOS pattern)."""
    fact = (fact_named or "").strip().lower()
    if fact in {
        "residence at the stated address",
        "whereabouts on a shared date",
        "authenticity of the offered exhibit",
        "authenticity of the exhibit offered to support the claim",
    }:
        return False
    fact_parts = {p.strip() for p in re.split(r"[,;/|]+", fact) if p.strip()}
    if fact_parts and fact_parts <= BOILERPLATE_STEMS:
        return True
    overlap = _stem_overlap(claim, evidence)
    meaningful = {s for s in overlap if s not in BOILERPLATE_STEMS}
    if overlap and not meaningful:
        return True
    # Classic USPS vs corporate charter collision.
    if re.search(r"(?i)\bunited\s+states\s+postal", claim) and re.search(
        r"(?i)\blaws?\s+of\s+the\s+united\s+states", evidence
    ):
        return True
    if re.search(r"(?i)\blaws?\s+of\s+the\s+united\s+states", claim) and re.search(
        r"(?i)\bunited\s+states\s+postal", evidence
    ):
        return True
    return False


def _street_numbers(text: str) -> set[str]:
    years = set(re.findall(r"\b(?:19|20)\d{2}\b", text or ""))
    return set(re.findall(r"\b\d{3,5}\b", text or "")) - years


def _same_street_number(claim: str, evidence: str) -> bool:
    left, right = _street_numbers(claim), _street_numbers(evidence)
    return bool(left and right and left & right)


def _different_places(claim: str, evidence: str) -> bool:
    left, right = _street_numbers(claim), _street_numbers(evidence)
    if left and right:
        return not bool(left & right)
    return True


def _denies_residence(text: str) -> bool:
    return bool(RESIDENCE_DENY.search(text or ""))


def _asserts_residence(text: str) -> bool:
    return bool(RESIDENCE_ASSERT.search(text or "")) and not _denies_residence(text or "")


def _caption_or_form(text: str) -> bool:
    return bool(CAPTION_OR_FORM.search(text or ""))


def _party_list_only(claim: str, evidence: str) -> bool:
    """Shared person-name tokens with a caption or party list are not a fact."""
    if not _caption_or_form(evidence) and not _caption_or_form(claim):
        return False
    overlap = _stem_overlap(claim, evidence)
    if not overlap:
        return True
    return not _predicate_overlap(claim, evidence)


def _characterization_vs_docket(claim: str, evidence: str) -> bool:
    return bool(OPPORTUNITY_CHAR.search(claim or "") and DOCKET_LINE.search(evidence or ""))


def _appearance_vs_service(claim: str, evidence: str) -> bool:
    return bool(
        APPEARANCE_FACT.search(claim or "")
        and (SERVICE_COMPLETED.search(evidence or "") or SERVICE_FACT.search(evidence or ""))
    )


def _appearance_vs_identity(claim: str, evidence: str) -> bool:
    """Default/appearance status is not the same fact as 'I am the defendant/attorney'."""
    return bool(APPEARANCE_FACT.search(claim or "") and ROLE_OR_IDENTITY.search(evidence or ""))


def _settlement_topic_only(claim: str, evidence: str) -> bool:
    """'Not settled' versus a document that merely names settlement as a topic."""
    if not SETTLEMENT_NEG.search(claim or ""):
        return False
    if SETTLEMENT_NEG.search(evidence or ""):
        return False
    if re.search(r"(?i)\b(case\s+settled|settlement\s+was\s+reached|settled\s+the\s+(?:case|action))\b", evidence or ""):
        return False
    return bool(re.search(r"(?i)\bsettlement\b", evidence or ""))


def _service_vs_address_listing(claim: str, evidence: str) -> bool:
    """A service-attempt claim is not the same fact as an address or identity printout."""
    denies_service = _is_denial(claim) and bool(SERVICE_FACT.search(claim))
    if not denies_service:
        return False
    if SERVICE_COMPLETED.search(evidence) or SERVICE_FACT.search(evidence):
        return False
    return _same_street_number(claim, evidence)


def _ordinary_traverse(claim: str, evidence: str) -> bool:
    """Competing service recitals are the ordinary traverse, not a gotcha.

    A denial versus an affidavit of service, a mailing form, or an address
    listing is the ordinary dispute. Two affidavits arguing about whether
    service was attempted is the same thing. A residence denial versus a
    record that lists that address can still be a gotcha — that is their
    exhibit undercutting them, not a traverse.
    """
    if _denies_residence(claim) and _same_street_number(claim, evidence) and not SERVICE_COMPLETED.search(evidence):
        return False
    claim_service = bool(SERVICE_FACT.search(claim))
    evidence_service = bool(SERVICE_COMPLETED.search(evidence) or SERVICE_FACT.search(evidence))
    if claim_service and evidence_service:
        return True
    denies_service = _is_denial(claim) and claim_service
    if not denies_service or _is_denial(evidence):
        return False
    return _same_street_number(claim, evidence)


def _process_form_variant(claim: str, evidence: str) -> bool:
    if MILITARY_FORM.search(claim or "") and MILITARY_FORM.search(evidence or ""):
        return True
    if MAILING_FORM.search(claim or "") and MAILING_FORM.search(evidence or ""):
        return True
    return False


def _corroborates(claim: str, evidence: str) -> bool:
    if SETTLEMENT_NEG.search(claim or "") and SETTLEMENT_NEG.search(evidence or ""):
        return True
    if (
        _asserts_residence(claim)
        and _same_street_number(claim, evidence)
        and not _denies_residence(evidence)
        and not SERVICE_COMPLETED.search(evidence)
    ):
        return True
    return False


def _incidental(claim: str, evidence: str) -> bool:
    blob = f"{claim} {evidence}".lower()
    if re.search(r"\b(typo|spelling|abbreviation|format)\b", blob):
        return True
    return False


def _same_calendar_date(claim: str, evidence: str) -> bool:
    """True when both quotes appear to name the same calendar day in different formats."""
    try:
        from dateutil import parser as dateparser
    except Exception:
        return False
    left = _parse_dates(claim, dateparser)
    right = _parse_dates(evidence, dateparser)
    return bool(left and right and left & right)


def _date_format_only(claim: str, evidence: str) -> bool:
    """Same calendar day written two ways, with no opposing fact attached."""
    if not _same_calendar_date(claim, evidence):
        return False
    if PLACE_FACT.search(claim) and PLACE_FACT.search(evidence):
        return False
    if _has_opposing_polarity(claim, evidence):
        return False
    return True


def _parse_dates(text: str, dateparser: Any) -> set[str]:
    found: set[str] = set()
    patterns = [
        r"\b\d{1,2}/\d{1,2}/\d{2,4}\b",
        r"\b(?:January|February|March|April|May|June|July|August|September|October|November|December)"
        r"\s+\d{1,2},?\s+\d{4}\b",
    ]
    for pattern in patterns:
        for match in re.findall(pattern, text, flags=re.I):
            try:
                parsed = dateparser.parse(match, fuzzy=False, dayfirst=False)
            except Exception:
                continue
            if parsed:
                found.add(parsed.date().isoformat())
    return found


def _local_type(claim: str) -> str:
    if re.search(r"(?i)\b(stipulation|settlement|order|notice|service)\b", claim):
        return "procedural_foundational"
    if PROCESS_DEFECT.search(claim):
        return "procedural_foundational"
    if re.search(r"(?i)\b(date|after|before|thereafter|weekday|afternoon|night)\b", claim):
        return "chronology_inconsistency"
    if EXCLUSIVE_OR_COMPLETE.search(claim) and re.search(r"(?i)\b(disclosed|provided|listed|accounts?)\b", claim):
        return "material_omission"
    return "factual_contradiction"


def _canonical_type(
    value: Any,
    fallback: dict[str, Any] | None = None,
    allow_retire_remap: bool = False,
) -> str | None:
    raw = str(value or "").strip().lower().replace(" ", "_").replace("-", "_")
    if raw in RETIRED_TYPES:
        if not allow_retire_remap:
            return None
        local = str((fallback or {}).get("contradiction_type") or "")
        return local if local in CONTRADICTION_TYPES else None
    if raw in MIGRATED_TYPES:
        return MIGRATED_TYPES[raw]
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
    local = str((fallback or {}).get("contradiction_type") or "")
    if local in CONTRADICTION_TYPES:
        return local
    return None if raw else "factual_contradiction"


def _normalize_type(value: Any) -> str:
    """Public alias used by scoring/eval. Never returns a retired label."""
    return _canonical_type(value) or "factual_contradiction"


def _first_fail(verdict: dict[str, Any]) -> str:
    for step in VERIFY_STEPS:
        block = verdict.get(step)
        if isinstance(block, dict) and block.get("pass") is False:
            return step
    return str(verdict.get("failed_step") or "same_fact")
