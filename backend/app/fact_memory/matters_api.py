"""Matter CRUD + findings/export for hosted fact memory (firm-scoped)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel, Field

from app.fact_memory.store import get_store
from app.v1_analysis import _require_analysis_auth, resolve_firm_context

matters_router = APIRouter(prefix="/v1/matters", tags=["matters"])


class MatterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    caption: str | None = None  # alias used by frontend createCase
    index_number: str | None = None
    court: str | None = None
    county: str | None = None
    notes: str | None = None


class FiledDatePatch(BaseModel):
    filed_date: str | None = None
    display_name: str | None = None
    doc_kind: str | None = None


def _ctx(auth: dict) -> dict[str, Any]:
    return resolve_firm_context(auth)


@matters_router.get("")
def list_matters(auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    rows = store.list_matters(ctx["firm_id"])
    store.audit(ctx["firm_id"], ctx.get("user_id"), "matter.list", None)
    # Shape compatible with frontend listCases → { cases: [...] }
    return {"matters": rows, "cases": rows}


@matters_router.post("")
def create_matter(body: MatterCreate, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    name = (body.name or body.caption or "Untitled matter").strip()
    matter = store.create_matter(
        ctx["firm_id"],
        name=name,
        index_number=body.index_number,
        court=body.court,
        county=body.county,
        user_id=ctx.get("user_id"),
    )
    return matter


@matters_router.post("/purge-firm")
def purge_firm(auth: dict = Depends(_require_analysis_auth)):
    """Hard-delete all fact memory for the caller's firm (account closure)."""
    ctx = _ctx(auth)
    store = get_store()
    ok = store.purge_firm(ctx["firm_id"], user_id=ctx.get("user_id"))
    if not ok:
        raise HTTPException(404, "Firm not found.")
    return {"purged": ctx["firm_id"]}


def _elevate_docs_for_conflicts(docs: list[dict], conflicts: list[dict]) -> list[dict]:
    """Overlay red/yellow on docs cited by material case fact conflicts (triage strip)."""
    rank = {"green": 0, "yellow": 1, "red": 2}
    bump: dict[str, str] = {}
    for conflict in conflicts or []:
        sev = str(conflict.get("severity") or "")
        if sev not in {"medium", "high", "critical"}:
            continue
        target = "yellow" if sev == "medium" else "red"
        ids: set[str] = set()
        for variant in conflict.get("variants") or []:
            for doc in variant.get("documents") or []:
                doc_id = doc.get("document_id")
                if doc_id:
                    ids.add(str(doc_id))
        for doc in conflict.get("related_documents") or []:
            doc_id = doc.get("document_id") or doc.get("id")
            if doc_id:
                ids.add(str(doc_id))
        for doc_id in ids:
            prev = bump.get(doc_id, "green")
            if rank.get(target, 0) > rank.get(prev, 0):
                bump[doc_id] = target
    if not bump:
        return docs
    out = []
    for doc in docs:
        row = dict(doc)
        target = bump.get(str(row.get("id") or ""))
        if target and rank.get(target, 0) > rank.get(str(row.get("risk_level") or "green"), 0):
            row["risk_level"] = target
        out.append(row)
    return out


@matters_router.get("/{matter_id}")
def get_matter(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    matter = store.get_matter(ctx["firm_id"], matter_id)
    if not matter:
        raise HTTPException(404, "Matter not found.")
    from app.case_consistency import aggregate_fact_conflicts

    docs = store.list_documents(ctx["firm_id"], matter_id)
    snaps = store.list_prior_snapshots(ctx["firm_id"], matter_id)
    conflicts = aggregate_fact_conflicts(snaps)
    docs = _elevate_docs_for_conflicts(docs, conflicts)
    findings = store.list_findings(ctx["firm_id"], matter_id)
    store.audit(ctx["firm_id"], ctx.get("user_id"), "matter.view", matter_id)
    return {
        **matter,
        "documents": docs,
        "findings": findings,
        # Frontend case shape aliases
        "caption": matter.get("name"),
        "archived_at": None,
    }


@matters_router.delete("/{matter_id}")
def delete_matter(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    ok = store.delete_matter(ctx["firm_id"], matter_id, user_id=ctx.get("user_id"))
    if not ok:
        raise HTTPException(404, "Matter not found.")
    return {"deleted": matter_id}


@matters_router.post("/{matter_id}/retitle")
def retitle_matter_documents(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    """Rebuild display names / kinds from original filenames (hosted Fix kinds)."""
    from app.auto_title import (
        looks_like_exhibit_filename,
        propose_document_title,
        uniquify_display_name,
    )

    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    docs = store.list_documents(ctx["firm_id"], matter_id)
    taken: list[str] = []
    changes: list[dict] = []
    for doc in docs:
        filename = doc.get("original_filename") or ""
        proposed, kind = propose_document_title(filename, "")
        if looks_like_exhibit_filename(filename) or looks_like_exhibit_filename(proposed):
            kind = "exhibit"
        unique = uniquify_display_name(proposed, taken, filename)
        taken.append(unique)
        fields: dict = {}
        if unique != (doc.get("display_name") or ""):
            fields["display_name"] = unique
        if kind and kind != (doc.get("doc_kind") or doc.get("doc_type")):
            fields["doc_type"] = kind
        if fields:
            store.update_document_labels(
                ctx["firm_id"],
                doc["id"],
                display_name=fields.get("display_name"),
                doc_type=fields.get("doc_type"),
                user_id=ctx.get("user_id"),
            )
            changes.append({"document_id": doc["id"], **fields})
    return {"updated": len(changes), "changes": changes}


@matters_router.get("/{matter_id}/retryable-jobs")
def list_retryable_jobs(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    """Failed/interrupted jobs eligible for client auto-retry from IndexedDB."""
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    rows = store.list_retryable_failed_jobs(
        firm_id=ctx["firm_id"], matter_id=matter_id, max_attempts=3
    )
    return {"jobs": rows}


@matters_router.get("/{matter_id}/findings")
def list_findings(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    from app.case_consistency import (
        aggregate_fact_conflicts,
        dedupe_gotchas,
        scrub_misattributed_employment_copy,
    )

    raw = store.list_findings(ctx["firm_id"], matter_id)
    store.audit(ctx["firm_id"], ctx.get("user_id"), "findings.view", matter_id)

    # One card per fact pair — persist can re-emit the same interrupt on every scan.
    findings = [
        scrub_misattributed_employment_copy(f)
        for f in dedupe_gotchas(raw)
    ]

    # Case-level aggregate cards (one per attribute cluster). Prefer these for
    # documentary fact conflicts so the board does not also list pairwise gotchas.
    snaps = store.list_prior_snapshots(ctx["firm_id"], matter_id)
    fact_conflicts = [
        scrub_misattributed_employment_copy(fc)
        for fc in aggregate_fact_conflicts(snaps)
        if str(fc.get("severity") or "") in {"high", "medium", "critical"}
    ]
    conflict_fields = {
        str(fc.get("field") or "").strip().lower()
        for fc in fact_conflicts
        if fc.get("field")
    }

    gotchas = []
    for f in findings:
        if f.get("severity") not in {"high", "medium", "critical"}:
            continue
        ev = f.get("evidence") or {}
        if ev.get("case_impact") == "noise":
            continue
        field = str(ev.get("field") or f.get("field") or "").strip().lower()
        # Skip documentary fact_conflict rows already covered by aggregate cards.
        if (
            (f.get("finding_type") == "fact_conflict" or ev.get("analysis_type") == "fact_conflict")
            and ev.get("documentary")
            and (not field or field in conflict_fields)
        ):
            continue
        gotchas.append(
            {
                **f,
                "category": "case_consistency"
                if f.get("finding_type") == "fact_conflict"
                else "gotcha",
                "why_it_may_matter": f.get("why_it_may_matter") or f.get("explanation"),
                "attorney_why": f.get("attorney_why") or ev.get("attorney_why"),
                "claim_quote": f.get("claim_quote") or ev.get("claim_quote"),
                "evidence_quote": f.get("evidence_quote") or ev.get("evidence_quote"),
                "fact_named": f.get("fact_named") or ev.get("fact_named"),
                "case_impact": ev.get("case_impact"),
                "case_impact_bucket": ev.get("case_impact_bucket"),
                "analysis_type": ev.get("analysis_type") or f.get("finding_type"),
                "verified": True,
            }
        )

    return {
        "findings": findings,
        "gotchas": gotchas,
        "fact_conflicts": fact_conflicts,
    }


@matters_router.get("/{matter_id}/export")
def export_matter(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    try:
        payload = store.export_matter(ctx["firm_id"], matter_id, user_id=ctx.get("user_id"))
    except KeyError as exc:
        raise HTTPException(404, "Matter not found.") from exc
    return payload


@matters_router.get("/{matter_id}/documents/{document_id}/report")
def get_document_report(
    matter_id: str, document_id: str, auth: dict = Depends(_require_analysis_auth)
):
    ctx = _ctx(auth)
    store = get_store()
    report = store.get_document_report(ctx["firm_id"], matter_id, document_id)
    if report is None:
        raise HTTPException(404, "Report not found.")
    store.audit(ctx["firm_id"], ctx.get("user_id"), "report.view", document_id)
    return report


@matters_router.get("/{matter_id}/documents/{document_id}/file")
def get_document_file(
    matter_id: str, document_id: str, auth: dict = Depends(_require_analysis_auth)
):
    """Exhibit bytes are not stored. The browser cache or a future vault holds the file."""
    raise HTTPException(
        status_code=410,
        detail="Exhibit files are not stored on the server.",
    )


@matters_router.post("/{matter_id}/documents/{document_id}/file")
def attach_document_file(
    matter_id: str,
    document_id: str,
    auth: dict = Depends(_require_analysis_auth),
):
    """Refuse uploads of exhibit bytes. The route does not read a body."""
    raise HTTPException(
        status_code=410,
        detail="Exhibit files are not stored on the server.",
    )


@matters_router.patch("/{matter_id}/documents/{document_id}")
def patch_document(
    matter_id: str,
    document_id: str,
    body: FiledDatePatch,
    auth: dict = Depends(_require_analysis_auth),
):
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    filed = body.filed_date
    if filed == "":
        filed = None
    if filed is not None:
        try:
            date.fromisoformat(filed)
        except ValueError as exc:
            raise HTTPException(400, "filed_date must be YYYY-MM-DD.") from exc
    doc = store.update_document_filed_date(
        ctx["firm_id"], document_id, filed, user_id=ctx.get("user_id")
    )
    if not doc:
        raise HTTPException(404, "Document not found.")
    return doc


@matters_router.delete("/{matter_id}/documents/{document_id}")
def delete_document(
    matter_id: str, document_id: str, auth: dict = Depends(_require_analysis_auth)
):
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    ok = store.delete_document(ctx["firm_id"], document_id, user_id=ctx.get("user_id"))
    if not ok:
        raise HTTPException(404, "Document not found.")
    return {"deleted": document_id}
