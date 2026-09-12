"""Answer attorney questions from the text already in a case.

Retrieves the most relevant excerpts (motions first when the question is about
purpose/use) and asks Gemini to answer only from those excerpts.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

from app import cases_db
from app.config import settings
from app.engines.contextual_logic import _generation_config, _parse_json_payload, _post_gemini
from app.ingestion import open_pdf_reader
from app.scan_profile import COURT_FILING_KINDS

STOP = frozenset(
    """
    the a an and or of to for in on at by with from as is was were be this that
    what which who whom whose why how where when was this used for exhibit
    document file case please tell me about can you
    """.split()
)

PURPOSE_RE = re.compile(
    r"(?i)\b(used for|purpose|why (?:was|is|did)|rely|relied|annexed|attached|"
    r"in support|offered|introduced|cite[sd]?|reference[sd]?)\b"
)

CHAT_SCHEMA = {
    "type": "object",
    "properties": {
        "answer": {"type": "string"},
        "citations": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "display_name": {"type": "string"},
                    "note": {"type": "string"},
                },
                "required": ["display_name", "note"],
            },
        },
    },
    "required": ["answer", "citations"],
}

CHAT_PROMPT = """You are a case clerk helping a busy attorney. Answer the question using ONLY
the case excerpts below. Quote short phrases when you rely on them. Name the source
document in the answer. If the excerpts do not contain the answer, say so clearly —
do not guess from outside knowledge.

When asked what an exhibit was used for, look in motions, affidavits, complaints, and
other pleadings for where that exhibit or issuer is cited (e.g. "Exhibit S", "Wells Fargo
statement", "in support of").

Question:
{question}

Excerpts from this case:
{packet}
"""


def ask_case(case_id: str, question: str, history: list[dict[str, str]] | None = None) -> dict[str, Any]:
    question = (question or "").strip()
    if not question:
        raise ValueError("Ask a question about the documents in this case.")
    case = cases_db.get_case(case_id)
    if case is None:
        raise ValueError("Unknown case.")

    extra_tokens: list[str] = []
    for turn in history or []:
        if turn.get("role") == "user":
            extra_tokens.extend(_tokens(turn.get("content") or ""))
    hits = retrieve(case_id, question, extra_tokens=extra_tokens)
    if not hits:
        return {
            "answer": "No extracted text in this case matches that question yet. Scan the filings first, then ask again.",
            "citations": [],
            "used_model": False,
        }

    packet = _format_packet(hits)
    if not settings.gemini_api_key:
        return {
            "answer": _fallback_answer(question, hits),
            "citations": _citations(hits),
            "used_model": False,
        }

    prior = ""
    if history:
        bits = []
        for turn in history[-4:]:
            role = "Attorney" if turn.get("role") == "user" else "Clerk"
            bits.append(f"{role}: {turn.get('content', '')[:400]}")
        prior = "Recent thread:\n" + "\n".join(bits) + "\n\n"

    try:
        body = _post_gemini(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [{"text": prior + CHAT_PROMPT.format(question=question, packet=packet)}],
                    }
                ],
                "generationConfig": _generation_config(CHAT_SCHEMA, settings.gemini_thinking_level),
            },
            timeout=max(settings.gemini_timeout_s, 45),
        )
        parsed = _parse_json_payload(body)
    except Exception:
        return {
            "answer": _fallback_answer(question, hits),
            "citations": _citations(hits),
            "used_model": False,
        }
    answer = str(parsed.get("answer") or "").strip()
    citations = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
    named = {c["display_name"]: c for c in _citations(hits)}
    merged = []
    for item in citations:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or "").strip()
        if not name:
            continue
        base = named.get(name) or next(
            (c for c in named.values() if name.lower() in c["display_name"].lower()),
            None,
        )
        merged.append(
            {
                "document_id": (base or {}).get("document_id"),
                "display_name": (base or {}).get("display_name") or name,
                "note": str(item.get("note") or "")[:240],
            }
        )
    if not merged:
        merged = _citations(hits)
    return {"answer": answer or _fallback_answer(question, hits), "citations": merged[:6], "used_model": True}


def retrieve(
    case_id: str,
    question: str,
    limit: int = 6,
    extra_tokens: list[str] | None = None,
) -> list[dict[str, Any]]:
    tokens = list(dict.fromkeys(_tokens(question) + list(extra_tokens or [])))
    purpose = bool(PURPOSE_RE.search(question))
    docs = cases_db.list_documents(case_id)
    snaps = {s["document_id"]: s for s in cases_db.list_case_snapshots(case_id)}
    ranked: list[tuple[float, dict[str, Any], dict[str, Any]]] = []
    for doc in docs:
        snap = snaps.get(doc["id"]) or {}
        findings = snap.get("findings_summary") or []
        finding_text = " ".join(
            f"{f.get('title', '')} {f.get('analysis_type', '')}"
            for f in findings
            if isinstance(f, dict)
        )
        cheap = " ".join(
            [
                doc.get("display_name") or "",
                doc.get("original_filename") or "",
                doc.get("doc_kind") or "",
                finding_text,
                (snap.get("text_excerpt") or "")[:2000],
            ]
        )
        score = _score(cheap, tokens, doc.get("doc_kind"), purpose)
        if purpose and (doc.get("doc_kind") or "").lower() in COURT_FILING_KINDS:
            score += 2
        if score <= 0 and not (purpose and (doc.get("doc_kind") or "").lower() in COURT_FILING_KINDS):
            continue
        ranked.append((score, doc, snap))
    ranked.sort(key=lambda item: item[0], reverse=True)

    scored: list[tuple[float, dict[str, Any]]] = []
    extracted = 0
    for cheap_score, doc, snap in ranked[:18]:
        cached = (snap.get("text_excerpt") or "").strip()
        text = cached
        if not text and extracted < 12:
            text = _live_text(doc.get("stored_path"))
            extracted += 1
            if text:
                cases_db.save_text_excerpt(doc["id"], case_id, text)
        findings = snap.get("findings_summary") or []
        blob = f"{text} {doc.get('display_name') or ''} {doc.get('original_filename') or ''}"
        score = _score(blob, tokens, doc.get("doc_kind"), purpose) or cheap_score
        if score <= 0:
            continue
        scored.append(
            (
                score,
                {
                    "document_id": doc["id"],
                    "display_name": doc.get("display_name") or doc.get("original_filename"),
                    "doc_kind": doc.get("doc_kind"),
                    "status": doc.get("status"),
                    "scan_job_id": doc.get("scan_job_id"),
                    "snippets": _snippets(text, tokens) or [text[:500] if text else ""],
                    "findings": [
                        f.get("title")
                        for f in findings[:6]
                        if isinstance(f, dict) and f.get("title")
                    ],
                },
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:limit] if (item[1].get("snippets") or [""])[0]]


def _live_text(stored_path: str | None, limit: int = 24_000) -> str:
    if not stored_path:
        return ""
    path = Path(stored_path)
    if not path.exists():
        return ""
    suffix = path.suffix.lower()
    try:
        if suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="ignore")[:limit]
        if suffix == ".pdf":
            reader = open_pdf_reader(path)
            chunks: list[str] = []
            total = 0
            for page in reader.pages[:40]:
                piece = page.extract_text() or ""
                chunks.append(piece)
                total += len(piece)
                if total >= limit:
                    break
            return "\n".join(chunks)[:limit]
    except Exception:
        return ""
    return ""


def _tokens(question: str) -> list[str]:
    words = re.findall(r"[a-z0-9][a-z0-9.&'-]{2,}", question.lower())
    out = [w for w in words if w not in STOP]
    return out or words[:6]


def _score(blob: str, tokens: list[str], kind: str | None, purpose: bool) -> float:
    hay = blob.lower()
    score = 0.0
    for token in tokens:
        score += hay.count(token)
    kind_l = (kind or "").lower()
    if purpose and kind_l in COURT_FILING_KINDS:
        score *= 1.6
    if kind_l in {"motion", "affirmation", "affidavit", "complaint", "memorandum"}:
        score *= 1.15
    return score


def _snippets(text: str, tokens: list[str], window: int = 420, limit: int = 3) -> list[str]:
    if not text:
        return []
    hay = text.lower()
    spans: list[tuple[int, int]] = []
    for token in tokens:
        start = 0
        while True:
            at = hay.find(token, start)
            if at < 0:
                break
            left = max(0, at - window // 3)
            right = min(len(text), at + len(token) + window)
            spans.append((left, right))
            start = at + len(token)
            if len(spans) >= 12:
                break
    merged: list[tuple[int, int]] = []
    for left, right in sorted(spans):
        if merged and left <= merged[-1][1] + 40:
            merged[-1] = (merged[-1][0], max(merged[-1][1], right))
        else:
            merged.append((left, right))
    out = []
    for left, right in merged[:limit]:
        chunk = re.sub(r"\s+", " ", text[left:right]).strip()
        if chunk:
            out.append(chunk)
    return out


def _format_packet(hits: list[dict[str, Any]], budget: int = 18_000) -> str:
    parts: list[str] = []
    used = 0
    for hit in hits:
        block = f"### {hit['display_name']} (kind: {hit.get('doc_kind') or 'unknown'})\n"
        if hit.get("findings"):
            block += "Scan notes: " + "; ".join(hit["findings"][:4]) + "\n"
        for snip in hit.get("snippets") or []:
            block += f"…{snip}…\n"
        if used + len(block) > budget:
            break
        parts.append(block)
        used += len(block)
    return "\n".join(parts)


def _citations(hits: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return [
        {
            "document_id": hit["document_id"],
            "display_name": hit["display_name"],
            "note": (hit.get("snippets") or [""])[0][:180],
        }
        for hit in hits[:5]
    ]


def _fallback_answer(question: str, hits: list[dict[str, Any]]) -> str:
    lines = [
        "I do not have a live model connection, so this is a direct excerpt match:",
    ]
    for hit in hits[:4]:
        snip = (hit.get("snippets") or [""])[0]
        lines.append(f"• {hit['display_name']}: {snip[:320]}")
    return "\n".join(lines)
