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
THIS_DOC_RE = re.compile(
    r"(?i)\b(this (exhibit|statement|document|file|page|one)|the exhibit|the statement)\b"
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
                    "document_id": {"type": "string"},
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
do not guess from outside knowledge. If text may be missing because a file was not
scanned yet, say so and suggest rescanning that file.

FIND / LOCATE questions (which document, where does it say, who mentioned X, carnival,
alibi, where was the defendant, etc.): lead with the document name, then a short
quoted phrase from that excerpt. Prefer the affidavit, answer, or exhibit that
actually contains the fact — not a caption or index alone.

When asked what an exhibit was used for, look in motions, affidavits, complaints, and
other pleadings for where that exhibit or issuer is cited (e.g. "Exhibit S", "Wells Fargo
statement", "in support of").

Cite every document you name. Use the exact heading after ###, include its document_id,
and include the NYSCEF Doc. No. when the excerpt has one so the attorney can open that file.

{focus_block}Question:
{question}

Excerpts from this case:
{packet}
"""

NYSCEF_RE = re.compile(r"(?i)NYSCEF\s+DOC(?:UMENT)?\.?\s*NO\.?\s*(\d+)")
TITLE_WITH_NYSCEF_RE = re.compile(
    r'["“]([^"”]{8,160})["”]\s*\((?:NYSCEF\s+)?Doc(?:ument)?\.?\s*No\.?\s*(\d+)\)',
    re.I,
)

FOCUS_BLOCK = """The attorney is viewing this document: {name} (kind: {kind}).
Treat “this exhibit,” “this statement,” and “this document” as that file.
Use its text and scan notes first. Also use other filings to explain how it was
cited or whether they conflict with it.

"""


def ask_case(
    case_id: str,
    question: str,
    history: list[dict[str, str]] | None = None,
    focus_document_id: str | None = None,
) -> dict[str, Any]:
    question = (question or "").strip()
    if not question:
        raise ValueError("Ask a question about the documents in this case.")
    case = cases_db.get_case(case_id)
    if case is None:
        raise ValueError("Unknown case.")

    focus = _resolve_focus(case_id, focus_document_id)
    extra_tokens: list[str] = []
    for turn in history or []:
        if turn.get("role") == "user":
            extra_tokens.extend(_tokens(turn.get("content") or ""))
    if focus:
        extra_tokens.extend(focus["tokens"])
    hits = retrieve(
        case_id,
        question,
        extra_tokens=extra_tokens,
        focus_document_id=focus["document"]["id"] if focus else None,
    )
    if not hits:
        return {
            "answer": "No extracted text in this case matches that question yet. Scan the filings first, then ask again.",
            "citations": [],
            "used_model": False,
        }

    packet = _format_packet(hits)
    if not settings.gemini_api_key:
        answer = _fallback_answer(question, hits)
        return {
            "answer": answer,
            "citations": finalize_citations(case_id, answer, [], hits),
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
                        "parts": [
                            {
                                "text": prior
                                + CHAT_PROMPT.format(
                                    question=question,
                                    packet=packet,
                                    focus_block=(
                                        FOCUS_BLOCK.format(
                                            name=focus["name"],
                                            kind=focus["kind"] or "unknown",
                                        )
                                        if focus
                                        else ""
                                    ),
                                )
                            }
                        ],
                    }
                ],
                "generationConfig": _generation_config(CHAT_SCHEMA, settings.gemini_thinking_level),
            },
            timeout=max(settings.gemini_timeout_s, 45),
        )
        parsed = _parse_json_payload(body)
    except Exception:
        answer = _fallback_answer(question, hits)
        return {
            "answer": answer,
            "citations": finalize_citations(case_id, answer, [], hits),
            "used_model": False,
        }
    answer = str(parsed.get("answer") or "").strip() or _fallback_answer(question, hits)
    model_cites = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
    return {
        "answer": answer,
        "citations": finalize_citations(case_id, answer, model_cites, hits),
        "used_model": True,
    }


def ask_case_from_packet(
    question: str,
    documents: list[dict[str, Any]],
    *,
    history: list[dict[str, str]] | None = None,
    focus_document_id: str | None = None,
) -> dict[str, Any]:
    """Ephemeral chat: answer from a client-supplied excerpt packet (no case DB / no vault)."""
    question = (question or "").strip()
    if not question:
        raise ValueError("Ask a question about the documents in this case.")
    packet_docs = []
    for row in documents or []:
        if not isinstance(row, dict):
            continue
        doc_id = str(row.get("document_id") or row.get("id") or "").strip()
        if not doc_id:
            continue
        packet_docs.append(
            {
                "id": doc_id,
                "display_name": row.get("display_name") or row.get("original_filename") or doc_id,
                "original_filename": row.get("original_filename") or "",
                "doc_kind": row.get("doc_kind") or "",
                "text_excerpt": str(row.get("text_excerpt") or "")[:24000],
                "findings_summary": row.get("findings_summary") or [],
                "status": row.get("status") or "complete",
            }
        )
    if not packet_docs:
        return {
            "answer": "No document excerpts were provided. Scan filings on this PC, then ask again.",
            "citations": [],
            "used_model": False,
        }

    focus_name = ""
    focus_kind = ""
    if focus_document_id:
        focus_row = next((d for d in packet_docs if d["id"] == focus_document_id), None)
        if focus_row:
            focus_name = focus_row["display_name"]
            focus_kind = focus_row.get("doc_kind") or ""

    extra_tokens: list[str] = []
    for turn in history or []:
        if turn.get("role") == "user":
            extra_tokens.extend(_tokens(turn.get("content") or ""))
    if focus_name:
        extra_tokens.extend(_tokens(focus_name))

    hits = retrieve_from_packet(
        packet_docs,
        question,
        extra_tokens=extra_tokens,
        focus_document_id=focus_document_id,
    )
    if not hits:
        return {
            "answer": (
                "No extracted text in the packet matches that question yet. "
                "Rescan waiting files so excerpts are available, then ask again."
            ),
            "citations": [],
            "used_model": False,
        }

    packet = _format_packet(hits)
    catalog = [
        {
            "document_id": d["id"],
            "id": d["id"],
            "display_name": d["display_name"],
            "nyscef_no": None,
        }
        for d in packet_docs
    ]

    if not settings.gemini_api_key:
        answer = _fallback_answer(question, hits)
        return {
            "answer": answer,
            "citations": finalize_citations_packet(answer, [], hits, catalog),
            "used_model": False,
        }

    prior = ""
    if history:
        bits = []
        for turn in history[-4:]:
            role = "Attorney" if turn.get("role") == "user" else "Clerk"
            bits.append(f"{role}: {turn.get('content', '')[:400]}")
        prior = "Recent thread:\n" + "\n".join(bits) + "\n\n"

    focus_block = (
        FOCUS_BLOCK.format(name=focus_name, kind=focus_kind or "unknown") if focus_name else ""
    )
    try:
        body = _post_gemini(
            {
                "contents": [
                    {
                        "role": "user",
                        "parts": [
                            {
                                "text": prior
                                + CHAT_PROMPT.format(
                                    question=question,
                                    packet=packet,
                                    focus_block=focus_block,
                                )
                            }
                        ],
                    }
                ],
                "generationConfig": _generation_config(CHAT_SCHEMA, settings.gemini_thinking_level),
            },
            timeout=max(settings.gemini_timeout_s, 45),
        )
        parsed = _parse_json_payload(body)
    except Exception:
        answer = _fallback_answer(question, hits)
        return {
            "answer": answer,
            "citations": finalize_citations_packet(answer, [], hits, catalog),
            "used_model": False,
        }
    answer = str(parsed.get("answer") or "").strip() or _fallback_answer(question, hits)
    model_cites = parsed.get("citations") if isinstance(parsed.get("citations"), list) else []
    return {
        "answer": answer,
        "citations": finalize_citations_packet(answer, model_cites, hits, catalog),
        "used_model": True,
    }


def retrieve_from_packet(
    docs: list[dict[str, Any]],
    question: str,
    limit: int = 6,
    extra_tokens: list[str] | None = None,
    focus_document_id: str | None = None,
) -> list[dict[str, Any]]:
    tokens = list(dict.fromkeys(_tokens(question) + list(extra_tokens or [])))
    purpose = bool(PURPOSE_RE.search(question) or (focus_document_id and THIS_DOC_RE.search(question)))
    scored: list[tuple[float, dict[str, Any]]] = []
    for doc in docs:
        text = str(doc.get("text_excerpt") or "")
        findings = doc.get("findings_summary") or []
        finding_text = " ".join(
            f"{f.get('title', '')} {f.get('analysis_type', '')}"
            for f in findings
            if isinstance(f, dict)
        )
        blob = " ".join(
            [
                doc.get("display_name") or "",
                doc.get("original_filename") or "",
                doc.get("doc_kind") or "",
                finding_text,
                text,
            ]
        )
        score = _score(blob, tokens, doc.get("doc_kind"), purpose)
        if purpose and (doc.get("doc_kind") or "").lower() in COURT_FILING_KINDS:
            score += 2
        focused = bool(focus_document_id and doc["id"] == focus_document_id)
        if focused:
            score += 8
        if score <= 0 and not focused:
            continue
        snippets = _snippets(text, tokens) or ([text[:500]] if text else [])
        if focused and text:
            lead = re.sub(r"\s+", " ", text[:900]).strip()
            if lead and lead not in snippets:
                snippets = [lead, *snippets][:4]
        scored.append(
            (
                1000 + score if focused else score,
                {
                    "document_id": doc["id"],
                    "display_name": doc.get("display_name") or doc.get("original_filename"),
                    "doc_kind": doc.get("doc_kind"),
                    "status": doc.get("status"),
                    "focused": focused,
                    "nyscef_no": _nyscef_no(doc, {}, text),
                    "snippets": snippets,
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


def finalize_citations_packet(
    answer: str,
    model_cites: list[Any],
    hits: list[dict[str, Any]],
    catalog: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Same citation shaping as finalize_citations, but catalog is the packet."""
    # Reuse finalize_citations matching by fabricating a fake case_id path via catalog inject.
    hit_by_id = {hit["document_id"]: hit for hit in hits}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(*, document_id: str | None, display_name: str, note: str = "", nyscef_no: str | None = None) -> None:
        match = None
        if document_id:
            match = next((c for c in catalog if c.get("document_id") == document_id or c.get("id") == document_id), None)
        if match is None:
            needle = (display_name or "").lower()
            match = next(
                (
                    c
                    for c in catalog
                    if needle and needle in (c.get("display_name") or "").lower()
                ),
                None,
            )
        if match is None and document_id and document_id in hit_by_id:
            match = hit_by_id[document_id]
        if match is None:
            return
        key = match.get("document_id") or match.get("id") or display_name
        if key in seen:
            return
        seen.add(key)
        stored_name = match.get("display_name") or display_name
        label = display_name if len(display_name) > 12 and display_name.lower() not in stored_name.lower() else stored_name
        no = nyscef_no or match.get("nyscef_no")
        if no and f"NYSCEF {no}" not in label:
            label = f"{label} (NYSCEF {no})"
        out.append(
            {
                "document_id": match.get("document_id") or match.get("id"),
                "display_name": label,
                "nyscef_no": no,
                "note": (note or match.get("note") or "")[:240],
            }
        )

    for title, number in TITLE_WITH_NYSCEF_RE.findall(answer or ""):
        add(document_id=None, display_name=title.strip(), nyscef_no=number, note="Named in the answer")
    for number in NYSCEF_RE.findall(answer or ""):
        add(document_id=None, display_name=f"NYSCEF Doc. No. {number}", nyscef_no=number, note="Named in the answer")
    for item in model_cites:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or "").strip()
        if not name:
            continue
        nos = NYSCEF_RE.findall(f"{name} {item.get('note') or ''}")
        add(
            document_id=str(item.get("document_id") or "") or None,
            display_name=name,
            note=str(item.get("note") or ""),
            nyscef_no=nos[0] if nos else None,
        )
    for hit in hits:
        if hit.get("focused") or (hit.get("display_name") and hit["display_name"].lower() in (answer or "").lower()):
            add(
                document_id=hit["document_id"],
                display_name=hit["display_name"],
                note=(hit.get("snippets") or [""])[0],
                nyscef_no=hit.get("nyscef_no"),
            )
    if not out:
        for hit in hits[:5]:
            add(
                document_id=hit["document_id"],
                display_name=hit["display_name"],
                note=(hit.get("snippets") or [""])[0],
                nyscef_no=hit.get("nyscef_no"),
            )
    return out[:8]


def retrieve(
    case_id: str,
    question: str,
    limit: int = 6,
    extra_tokens: list[str] | None = None,
    focus_document_id: str | None = None,
) -> list[dict[str, Any]]:
    tokens = list(dict.fromkeys(_tokens(question) + list(extra_tokens or [])))
    purpose = bool(PURPOSE_RE.search(question) or (focus_document_id and THIS_DOC_RE.search(question)))
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
        focused = bool(focus_document_id and doc["id"] == focus_document_id)
        if focused:
            score += 8
        if (
            score <= 0
            and not focused
            and not (purpose and (doc.get("doc_kind") or "").lower() in COURT_FILING_KINDS)
        ):
            continue
        ranked.append((score, doc, snap))
    ranked.sort(key=lambda item: item[0], reverse=True)

    if focus_document_id and not any(doc["id"] == focus_document_id for _, doc, _ in ranked):
        focus_doc = next((doc for doc in docs if doc["id"] == focus_document_id), None)
        if focus_doc:
            ranked.insert(0, (20, focus_doc, snaps.get(focus_document_id) or {}))

    scored: list[tuple[float, dict[str, Any]]] = []
    extracted = 0
    for cheap_score, doc, snap in ranked[:18]:
        cached = (snap.get("text_excerpt") or "").strip()
        text = cached
        focused = bool(focus_document_id and doc["id"] == focus_document_id)
        if not text and (focused or extracted < 12):
            text = _live_text(doc.get("stored_path"), limit=28_000 if focused else 24_000)
            extracted += 1
            if text:
                cases_db.save_text_excerpt(doc["id"], case_id, text)
        findings = snap.get("findings_summary") or []
        blob = f"{text} {doc.get('display_name') or ''} {doc.get('original_filename') or ''}"
        score = _score(blob, tokens, doc.get("doc_kind"), purpose) or cheap_score
        if score <= 0 and not focused:
            continue
        snippets = _snippets(text, tokens) or ([text[:500]] if text else [])
        if focused and text:
            lead = re.sub(r"\s+", " ", text[:900]).strip()
            if lead and lead not in snippets:
                snippets = [lead, *snippets][:4]
        scored.append(
            (
                1000 + score if focused else score,
                {
                    "document_id": doc["id"],
                    "display_name": doc.get("display_name") or doc.get("original_filename"),
                    "doc_kind": doc.get("doc_kind"),
                    "status": doc.get("status"),
                    "scan_job_id": doc.get("scan_job_id"),
                    "focused": focused,
                    "nyscef_no": _nyscef_no(doc, snap, text),
                    "snippets": snippets,
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


def _resolve_focus(case_id: str, document_id: str | None) -> dict[str, Any] | None:
    if not document_id:
        return None
    doc = cases_db.get_document(document_id)
    if not doc or doc.get("case_id") != case_id:
        return None
    snap = cases_db.get_scan_snapshot(document_id) or {}
    name = doc.get("display_name") or doc.get("original_filename") or "this document"
    return {
        "document": doc,
        "snap": snap,
        "name": name,
        "kind": doc.get("doc_kind") or "",
        "tokens": _document_tokens(doc, snap),
    }


def _document_tokens(doc: dict[str, Any], snap: dict[str, Any]) -> list[str]:
    bits = [doc.get("display_name") or "", doc.get("original_filename") or ""]
    orgs = (snap.get("entities") or {}).get("organizations") or []
    if isinstance(orgs, list):
        bits.extend(str(org) for org in orgs[:8])
    for item in snap.get("findings_summary") or []:
        if isinstance(item, dict):
            bits.append(str(item.get("title") or ""))
    return _tokens(" ".join(bits))


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
    ordered = sorted(hits, key=lambda hit: 0 if hit.get("focused") else 1)
    parts: list[str] = []
    used = 0
    for hit in ordered:
        label = "FOCUS — " if hit.get("focused") else ""
        nyscef = f", NYSCEF {hit['nyscef_no']}" if hit.get("nyscef_no") else ""
        block = (
            f"### {label}{hit['display_name']} "
            f"[document_id={hit['document_id']}] "
            f"(kind: {hit.get('doc_kind') or 'unknown'}{nyscef})\n"
        )
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
            "nyscef_no": hit.get("nyscef_no"),
            "note": (hit.get("snippets") or [""])[0][:180],
        }
        for hit in hits[:5]
    ]


def finalize_citations(
    case_id: str,
    answer: str,
    model_cites: list[Any],
    hits: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    catalog = _doc_catalog(case_id)
    hit_by_id = {hit["document_id"]: hit for hit in hits}
    out: list[dict[str, Any]] = []
    seen: set[str] = set()

    def add(
        *,
        document_id: str | None,
        display_name: str,
        note: str = "",
        nyscef_no: str | None = None,
    ) -> None:
        match = _match_catalog(catalog, document_id=document_id, name=display_name, nyscef_no=nyscef_no)
        if match is None and document_id and document_id in hit_by_id:
            match = hit_by_id[document_id]
        if match is None:
            return
        key = match.get("document_id") or match.get("id") or display_name
        if key in seen:
            return
        seen.add(key)
        stored_name = match.get("display_name") or display_name
        label = display_name if len(display_name) > 12 and display_name.lower() not in stored_name.lower() else stored_name
        no = nyscef_no or match.get("nyscef_no")
        if no and f"NYSCEF {no}" not in label:
            label = f"{label} (NYSCEF {no})"
        out.append(
            {
                "document_id": match.get("document_id") or match.get("id"),
                "display_name": label,
                "nyscef_no": no,
                "note": (note or match.get("note") or "")[:240],
            }
        )

    for title, number in TITLE_WITH_NYSCEF_RE.findall(answer or ""):
        add(document_id=None, display_name=title.strip(), nyscef_no=number, note="Named in the answer")
    for number in NYSCEF_RE.findall(answer or ""):
        add(document_id=None, display_name=f"NYSCEF Doc. No. {number}", nyscef_no=number, note="Named in the answer")
    for item in model_cites:
        if not isinstance(item, dict):
            continue
        name = str(item.get("display_name") or "").strip()
        if not name:
            continue
        nos = NYSCEF_RE.findall(f"{name} {item.get('note') or ''}")
        add(
            document_id=str(item.get("document_id") or "") or None,
            display_name=name,
            note=str(item.get("note") or ""),
            nyscef_no=nos[0] if nos else None,
        )
    for hit in hits:
        if hit.get("focused") or (hit.get("display_name") and hit["display_name"].lower() in (answer or "").lower()):
            add(
                document_id=hit["document_id"],
                display_name=hit["display_name"],
                note=(hit.get("snippets") or [""])[0],
                nyscef_no=hit.get("nyscef_no"),
            )
    if not out:
        for hit in hits[:5]:
            add(
                document_id=hit["document_id"],
                display_name=hit["display_name"],
                note=(hit.get("snippets") or [""])[0],
                nyscef_no=hit.get("nyscef_no"),
            )
    return out[:8]


def _nyscef_no(doc: dict[str, Any], snap: dict[str, Any], text: str = "") -> str | None:
    hay = " ".join(
        [
            doc.get("display_name") or "",
            doc.get("original_filename") or "",
            text[:4000],
            (snap.get("text_excerpt") or "")[:4000],
        ]
    )
    found = NYSCEF_RE.findall(hay)
    if found:
        return found[0]
    tail = re.search(r"(?i)(?:affirm|affidavit|motion|exhibit|order|complaint|doc)\w*\s+(\d{1,4})\s*$", (doc.get("display_name") or "").strip())
    if tail:
        return tail.group(1)
    return None


def _doc_catalog(case_id: str) -> list[dict[str, Any]]:
    snaps = {item["document_id"]: item for item in cases_db.list_case_snapshots(case_id)}
    catalog = []
    for doc in cases_db.list_documents(case_id):
        snap = snaps.get(doc["id"]) or {}
        excerpt = snap.get("text_excerpt") or ""
        catalog.append(
            {
                "id": doc["id"],
                "document_id": doc["id"],
                "display_name": doc.get("display_name") or doc.get("original_filename"),
                "original_filename": doc.get("original_filename") or "",
                "nyscef_no": _nyscef_no(doc, snap, excerpt),
                "hay": " ".join(
                    [
                        doc.get("display_name") or "",
                        doc.get("original_filename") or "",
                        excerpt[:2500],
                    ]
                ).lower(),
            }
        )
    return catalog


def _match_catalog(
    catalog: list[dict[str, Any]],
    *,
    document_id: str | None = None,
    name: str | None = None,
    nyscef_no: str | None = None,
) -> dict[str, Any] | None:
    if document_id:
        hit = next((item for item in catalog if item["document_id"] == document_id), None)
        if hit:
            return hit
    if nyscef_no:
        hit = next((item for item in catalog if item.get("nyscef_no") == str(nyscef_no)), None)
        if hit:
            return hit
        hit = next(
            (
                item
                for item in catalog
                if re.search(rf"(?i)(?:^|[\s_\-]){re.escape(str(nyscef_no))}(?:\.pdf)?$", (item.get("display_name") or "").strip())
                or re.search(rf"(?i)(?:affirm|affidavit|motion|exhibit|order|complaint)\w*\s+{re.escape(str(nyscef_no))}\b", item.get("hay") or "")
            ),
            None,
        )
        if hit:
            return hit
    query = (name or "").strip().lower()
    if len(query) < 6:
        return None
    exact = next((item for item in catalog if (item.get("display_name") or "").lower() == query), None)
    if exact:
        return exact
    contained = next((item for item in catalog if query in item["hay"] or item["hay"] in query), None)
    if contained:
        return contained
    words = [w for w in re.findall(r"[a-z0-9']{4,}", query) if w not in STOP]
    if len(words) < 2:
        return None
    best = None
    best_score = 0
    for item in catalog:
        score = sum(1 for word in words if word in item["hay"])
        if score > best_score:
            best = item
            best_score = score
    if best and best_score >= max(2, len(words) - 1):
        return best
    return None


def _fallback_answer(question: str, hits: list[dict[str, Any]]) -> str:
    lines = [
        "I do not have a live model connection, so this is a direct excerpt match:",
    ]
    for hit in hits[:4]:
        snip = (hit.get("snippets") or [""])[0]
        lines.append(f"• {hit['display_name']}: {snip[:320]}")
    return "\n".join(lines)
