"""Matter CRUD + findings/export for hosted fact memory (firm-scoped)."""

from __future__ import annotations

from datetime import date
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.fact_memory.store import get_store
from app.recompute_jobs import get_recompute_job, start_recompute_job
from app.v1_analysis import _require_analysis_auth, require_entitled, resolve_firm_context

matters_router = APIRouter(prefix="/v1/matters", tags=["matters"])


class MatterCreate(BaseModel):
    name: str = Field(min_length=1, max_length=512)
    caption: str | None = None  # alias used by frontend createCase
    index_number: str | None = None
    court: str | None = None
    county: str | None = None
    notes: str | None = None


class MatterPatch(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=512)
    caption: str | None = Field(default=None, min_length=1, max_length=512)
    index_number: str | None = None
    court: str | None = None
    county: str | None = None


class FiledDatePatch(BaseModel):
    filed_date: str | None = None
    display_name: str | None = None
    doc_kind: str | None = None


def _ctx(auth: dict) -> dict[str, Any]:
    return resolve_firm_context(auth)


def _request_id(request: Request) -> str:
    incoming = (request.headers.get("x-request-id") or "").strip()
    if incoming:
        return incoming[:64]
    import uuid

    return uuid.uuid4().hex


@matters_router.get("")
def list_matters(auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    rows = store.list_matters(ctx["firm_id"])
    store.audit(ctx["firm_id"], ctx.get("user_id"), "matter.list", None)
    # Shape compatible with frontend listCases → { cases: [...] }
    return {"matters": rows, "cases": rows}


def _require_firm_admin(ctx: dict[str, Any]) -> str:
    store = get_store()
    role = store.member_role(ctx["firm_id"], ctx.get("user_id"))
    if role not in {"owner", "admin"}:
        raise HTTPException(status_code=403, detail="Firm admin access is required.")
    return role


@matters_router.get("/audit")
def list_firm_audit(auth: dict = Depends(_require_analysis_auth)):
    """Firm owners and admins read their own access log. Other firms are not included."""
    ctx = _ctx(auth)
    _require_firm_admin(ctx)
    store = get_store()
    events = [
        {
            "action": row.get("action"),
            "target": row.get("target"),
            "request_id": row.get("request_id"),
            "at": row.get("at"),
        }
        for row in store.list_audit(ctx["firm_id"], limit=100)
    ]
    return {"events": events}


class FirmSettingsPatch(BaseModel):
    name: str | None = Field(default=None, max_length=255)
    persist_facts: bool | None = None
    store_snippets: bool | None = None
    retention_policy: str | None = Field(default=None, max_length=64)


@matters_router.get("/admin")
def firm_admin_overview(auth: dict = Depends(_require_analysis_auth)):
    """Firm admin panel: members, settings, scan/job health (firm-scoped)."""
    ctx = _ctx(auth)
    role = _require_firm_admin(ctx)
    store = get_store()
    firm = store.get_firm(ctx["firm_id"])
    if not firm:
        raise HTTPException(404, "Firm not found.")
    try:
        members = store.list_members(ctx["firm_id"])
    except KeyError:
        members = []
    try:
        jobs = store.job_health_summary(ctx["firm_id"])
    except KeyError:
        jobs = {"total": 0, "active": 0, "by_status": {}, "recent": []}
    store.audit(ctx["firm_id"], ctx.get("user_id"), "admin.overview", ctx["firm_id"])
    return {
        "role": role,
        "firm": firm,
        "members": members,
        "jobs": jobs,
    }


@matters_router.patch("/admin/settings")
def firm_admin_settings(body: FirmSettingsPatch, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    _require_firm_admin(ctx)
    store = get_store()
    try:
        firm = store.update_firm_settings(
            ctx["firm_id"],
            name=body.name,
            persist_facts=body.persist_facts,
            store_snippets=body.store_snippets,
            retention_policy=body.retention_policy,
            user_id=ctx.get("user_id"),
        )
    except KeyError:
        raise HTTPException(404, "Firm not found.") from None
    return {"firm": firm}


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


@matters_router.patch("/{matter_id}")
def patch_matter(matter_id: str, body: MatterPatch, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    name = body.name if body.name is not None else body.caption
    updated = store.update_matter(
        ctx["firm_id"],
        matter_id,
        name=name,
        index_number=body.index_number,
        court=body.court,
        county=body.county,
        user_id=ctx.get("user_id"),
    )
    if updated is None:
        raise HTTPException(404, "Matter not found.")
    return updated


@matters_router.post("/purge-firm")
def purge_firm(auth: dict = Depends(_require_analysis_auth)):
    """Hard-delete all fact memory for the caller's firm (account closure)."""
    ctx = _ctx(auth)
    store = get_store()
    ok = store.purge_firm(ctx["firm_id"], user_id=ctx.get("user_id"))
    if not ok:
        raise HTTPException(404, "Firm not found.")
    return {"purged": ctx["firm_id"]}


def _overlay_open_scans(store, firm_id: str, matter_id: str, docs: list[dict]) -> list[dict]:
    """Drop Complete / risk on the list while a newer scan job is still open.

    Also appends provisional rows for accepted uploads whose Document row does
    not exist yet (hosted jobs persist after analysis).
    """
    from app.scan_row import overlay_inflight_scans
    from app.storage import load_progress

    try:
        jobs = store.latest_jobs_by_sha(firm_id, matter_id)
    except Exception:
        return docs
    _active = {"queued", "running", "local_done"}
    _failed = {"failed", "interrupted"}
    for job in jobs.values():
        durable = str(job.get("status") or "")
        if durable not in _active | _failed | {"complete"}:
            continue
        try:
            progress = load_progress(str(job.get("job_id") or ""))
        except Exception:
            progress = None
        if progress is None:
            continue
        job["stage"] = progress.stage
        fname = getattr(progress, "filename", None)
        if fname and not job.get("original_filename"):
            job["original_filename"] = fname
        if fname and not job.get("display_name"):
            job["display_name"] = fname
        if fname and not job.get("filename"):
            job["filename"] = fname
        pstatus = str(progress.status or "")
        # Prefer durable active/failed status. progress.json must not flip an
        # open job to "complete" and skip the in-flight overlay.
        if durable in _active:
            if pstatus in _active:
                job["status"] = pstatus
            elif pstatus in _failed:
                job["status"] = pstatus
            # else keep durable active
        elif durable in _failed:
            job["status"] = durable
        elif pstatus in _active | _failed | {"complete"}:
            job["status"] = pstatus
        if progress.error and not job.get("error"):
            job["error"] = progress.error
        live_modules = getattr(progress, "scan_modules", None)
        if isinstance(live_modules, list) and live_modules:
            job["scan_modules"] = live_modules
    return overlay_inflight_scans(docs, jobs)


def _elevate_docs_for_conflicts(docs: list[dict], conflicts: list[dict]) -> list[dict]:
    """Mark docs cited by case conflicts without rewriting forensic score/level.

    Case-impact cards live in findings; each document's risk_level must match
    its own risk_score (no High label at score 0).
    """
    from app.report.scoring import risk_level_from_score

    cited: set[str] = set()
    for conflict in conflicts or []:
        sev = str(conflict.get("severity") or "")
        if sev not in {"medium", "high", "critical"}:
            continue
        for variant in conflict.get("variants") or []:
            for doc in variant.get("documents") or []:
                doc_id = doc.get("document_id")
                if doc_id:
                    cited.add(str(doc_id))
        for doc in conflict.get("related_documents") or []:
            doc_id = doc.get("document_id") or doc.get("id")
            if doc_id:
                cited.add(str(doc_id))
    out = []
    for doc in docs:
        row = dict(doc)
        scored = risk_level_from_score(row.get("risk_score"))
        stored = str(row.get("risk_level") or "")
        # A required visual review that did not finish stays uncleared even
        # when the numeric score would otherwise read as low risk.
        if scored == "green" and (
            stored == "incomplete" or row.get("ai_review") == "incomplete"
        ):
            row["risk_level"] = "incomplete"
        else:
            row["risk_level"] = scored
        if str(row.get("id") or "") in cited:
            row["case_conflict_cited"] = True
        out.append(row)
    return out


@matters_router.get("/{matter_id}")
def get_matter(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    matter = store.get_matter(ctx["firm_id"], matter_id)
    if not matter:
        raise HTTPException(404, "Matter not found.")
    from cryptography.fernet import InvalidToken

    from app.case_conflict_toggle import case_fact_conflicts_enabled, filter_findings
    from app.case_consistency import aggregate_fact_conflicts

    try:
        docs = store.list_documents(ctx["firm_id"], matter_id)
        conflicts: list[dict] = []
        if case_fact_conflicts_enabled():
            snaps = store.list_prior_snapshots(ctx["firm_id"], matter_id)
            conflicts = aggregate_fact_conflicts(snaps)
        docs = _elevate_docs_for_conflicts(docs, conflicts)
        docs = _overlay_open_scans(store, ctx["firm_id"], matter_id, docs)
        findings = filter_findings(store.list_findings(ctx["firm_id"], matter_id))
    except InvalidToken as exc:
        # Master-key mismatch: list works (no unwrap) but open decrypts the firm key.
        raise HTTPException(
            503,
            "Matter storage key mismatch. Set FACT_MEMORY_MASTER_KEY to the key "
            "used when this firm was created, then restart analysis.",
        ) from exc
    store.audit(ctx["firm_id"], ctx.get("user_id"), "matter.view", matter_id)
    # Full list — never silently truncate. Client must treat incomplete as error.
    return {
        **matter,
        "documents": docs,
        "documents_total": len(docs),
        "documents_complete": True,
        "findings": findings,
        # Frontend case shape aliases
        "caption": matter.get("name"),
        "archived_at": None,
    }


@matters_router.delete("/{matter_id}")
def delete_matter(
    matter_id: str,
    request: Request,
    auth: dict = Depends(_require_analysis_auth),
):
    ctx = _ctx(auth)
    store = get_store()
    ok = store.delete_matter(
        ctx["firm_id"],
        matter_id,
        user_id=ctx.get("user_id"),
        request_id=_request_id(request),
    )
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
    exhausted = store.list_exhausted_jobs(
        firm_id=ctx["firm_id"], matter_id=matter_id, max_attempts=3
    )
    return {"jobs": rows, "exhausted": exhausted}


@matters_router.post("/{matter_id}/recompute-facts")
def recompute_matter_facts(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    """Start async recompute from stored text excerpts. Returns 202 + job_id."""
    require_entitled(auth)
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    row = start_recompute_job(
        firm_id=ctx["firm_id"],
        matter_id=matter_id,
        user_id=ctx.get("user_id"),
    )
    return JSONResponse(status_code=202, content=row)


@matters_router.get("/{matter_id}/recompute-facts/{job_id}")
def get_recompute_matter_facts(
    matter_id: str, job_id: str, auth: dict = Depends(_require_analysis_auth)
):
    """Poll async recompute job status (progress + before/after when complete)."""
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    row = get_recompute_job(job_id)
    if not row or row.get("matter_id") != matter_id or row.get("firm_id") != ctx["firm_id"]:
        raise HTTPException(404, "Recompute job not found.")
    return row


def _case_impact_board(store, firm_id: str, matter_id: str) -> dict[str, Any]:
    """Gotchas + fact conflicts for Case Impact / partner export (same filter as UI)."""
    from app.case_conflict_toggle import case_fact_conflicts_enabled, filter_findings
    from app.case_consistency import (
        aggregate_fact_conflicts,
        dedupe_gotchas,
        scrub_misattributed_employment_copy,
    )

    raw = filter_findings(store.list_findings(firm_id, matter_id))
    # One card per fact pair — persist can re-emit the same interrupt on every scan.
    findings = [
        scrub_misattributed_employment_copy(f)
        for f in dedupe_gotchas(raw)
    ]

    # Case-level aggregate cards (one per attribute cluster). Prefer these for
    # documentary fact conflicts so the board does not also list pairwise gotchas.
    # Hidden (and not computed) unless CASE_FACT_CONFLICTS=true.
    fact_conflicts: list[dict] = []
    if case_fact_conflicts_enabled():
        snaps = store.list_prior_snapshots(firm_id, matter_id)
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
                "document_id": f.get("document_id") or ev.get("document_id"),
                "page": f.get("page") if f.get("page") is not None else ev.get("page"),
                "explanation": f.get("explanation"),
                "why_suspicious": f.get("why_suspicious") or ev.get("why_suspicious"),
                "why_it_may_matter": f.get("why_it_may_matter") or f.get("explanation"),
                "attorney_why": f.get("attorney_why") or ev.get("attorney_why"),
                "claim_quote": f.get("claim_quote") or ev.get("claim_quote"),
                "claim_source": f.get("claim_source") or ev.get("claim_source"),
                "evidence_quote": (
                    f.get("evidence_quote")
                    or ev.get("evidence_quote")
                    or ev.get("quoted_evidence")
                ),
                "evidence_source": f.get("evidence_source") or ev.get("evidence_source"),
                "evidence_label": f.get("evidence_label") or ev.get("evidence_label"),
                "materiality_reason": f.get("materiality_reason") or ev.get("materiality_reason"),
                "fact_named": f.get("fact_named") or ev.get("fact_named"),
                "tainted_support": bool(
                    f.get("tainted_support") or ev.get("tainted_support")
                ),
                "self_undercut": bool(f.get("self_undercut") or ev.get("self_undercut")),
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


@matters_router.get("/{matter_id}/findings")
def list_findings(matter_id: str, auth: dict = Depends(_require_analysis_auth)):
    ctx = _ctx(auth)
    store = get_store()
    if not store.get_matter(ctx["firm_id"], matter_id):
        raise HTTPException(404, "Matter not found.")
    store.audit(ctx["firm_id"], ctx.get("user_id"), "findings.view", matter_id)
    return _case_impact_board(store, ctx["firm_id"], matter_id)


@matters_router.get("/{matter_id}/export")
def export_matter(
    matter_id: str,
    request: Request,
    auth: dict = Depends(_require_analysis_auth),
):
    """Partner review pack: matter metadata, doc risk strip, Case Impact, material findings."""
    ctx = _ctx(auth)
    store = get_store()
    try:
        payload = store.export_matter(
            ctx["firm_id"],
            matter_id,
            user_id=ctx.get("user_id"),
            request_id=_request_id(request),
        )
    except KeyError as exc:
        raise HTTPException(404, "Matter not found.") from exc

    board = _case_impact_board(store, ctx["firm_id"], matter_id)
    docs = payload.get("documents") or []
    material_findings = [
        f
        for f in (board.get("findings") or [])
        if f.get("severity") in {"high", "medium", "critical"}
        and (f.get("evidence") or {}).get("case_impact") != "noise"
    ]
    payload["document_summary"] = [
        {
            "id": d.get("id"),
            "display_name": d.get("display_name") or d.get("original_filename"),
            "doc_kind": d.get("doc_kind") or d.get("doc_type"),
            "status": d.get("status"),
            "risk_level": d.get("risk_level"),
            "risk_score": d.get("risk_score"),
            "has_report": bool(d.get("has_report")),
            "ai_review": d.get("ai_review"),
            "sha256": d.get("sha256"),
        }
        for d in docs
    ]
    payload["case_impact"] = {
        "material_count": len(board.get("gotchas") or [])
        + len(board.get("fact_conflicts") or []),
        "gotchas": board.get("gotchas") or [],
        "fact_conflicts": board.get("fact_conflicts") or [],
    }
    # Prefer board-filtered material findings over raw (incl. stale) store dump.
    payload["findings"] = material_findings
    payload["export_kind"] = "matter_review_pack"
    return payload


@matters_router.get("/{matter_id}/documents/{document_id}/report")
def get_document_report(
    matter_id: str, document_id: str, auth: dict = Depends(_require_analysis_auth)
):
    ctx = _ctx(auth)
    store = get_store()
    from app.case_conflict_toggle import strip_report

    report = store.get_document_report(ctx["firm_id"], matter_id, document_id)
    if report is None:
        raise HTTPException(404, "Report not found.")
    store.audit(ctx["firm_id"], ctx.get("user_id"), "report.view", document_id)
    return strip_report(report)


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
    doc = None
    # Only write filed_date when the client sent that field. A rename or kind
    # change must not clear a date the user already saved.
    if "filed_date" in body.model_fields_set:
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
    if body.display_name is not None or "doc_kind" in body.model_fields_set:
        labeled = store.update_document_labels(
            ctx["firm_id"],
            document_id,
            display_name=body.display_name,
            doc_type=body.doc_kind if "doc_kind" in body.model_fields_set else None,
            user_id=ctx.get("user_id"),
        )
        if not labeled and doc is None:
            raise HTTPException(404, "Document not found.")
        if labeled:
            doc = labeled
    if doc is None:
        doc = store.get_document(ctx["firm_id"], document_id)
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
