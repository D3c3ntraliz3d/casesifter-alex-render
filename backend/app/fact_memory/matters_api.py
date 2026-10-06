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


@matters_router.get("/{matter_id}")
def get_matter(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    matter = store.get_matter(ctx["firm_id"], matter_id)
    if not matter:
        raise HTTPException(404, "Matter not found.")
    docs = store.list_documents(ctx["firm_id"], matter_id)
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


@matters_router.get("/{matter_id}/findings")
def list_findings(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    findings = store.list_findings(ctx["firm_id"], matter_id)
    store.audit(ctx["firm_id"], ctx.get("user_id"), "findings.view", matter_id)
    # Map to gotchas-ish shape for the Cases UI board.
    gotchas = [
        {
            **f,
            "category": "case_consistency" if f.get("finding_type") == "fact_conflict" else "gotcha",
            "why_it_may_matter": f.get("explanation"),
        }
        for f in findings
        if f.get("severity") in {"high", "medium", "critical"}
    ]
    return {
        "findings": findings,
        "gotchas": gotchas,
        "fact_conflicts": [
            f for f in findings if (f.get("evidence") or {}).get("documentary")
        ],
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
