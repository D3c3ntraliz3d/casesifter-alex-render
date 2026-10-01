"""Public v1 analysis API for the thin desktop client (ephemeral jobs)."""

from __future__ import annotations

import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, Depends, File, Form, Header, HTTPException, UploadFile
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field

from app.case_chat import ask_case_from_packet
from app.config import settings
from app.entitlement import require_scan_allowed
from app.ephemeral import delete_job_files, ensure_sweeper, maybe_delete_after_terminal, sweep_stale_jobs
from app.models import UploadResponse
from app.pipeline import run_pipeline
from app.product_events import emit_chat_asked, emit_scan_failed
from app.scan_slots import acquire as acquire_scan_slot
from app.scan_slots import release as release_scan_slot
from app.scan_watchdog import ensure_watchdog
from app.storage import create_job, job_dir, load_progress, load_report, update_job

v1 = APIRouter(prefix="/v1", tags=["v1-analysis"])


class CaseChatPacketDoc(BaseModel):
    document_id: str = Field(min_length=1, max_length=80)
    display_name: str | None = None
    original_filename: str | None = None
    doc_kind: str | None = None
    text_excerpt: str = ""
    findings_summary: list[dict] | None = None
    status: str | None = None


class CaseChatPacketBody(BaseModel):
    question: str = Field(min_length=1, max_length=2000)
    focus_document_id: str | None = None
    history: list[dict[str, str]] | None = None
    documents: list[CaseChatPacketDoc] = Field(default_factory=list, max_length=80)


class FactConflictPacketDoc(BaseModel):
    document_id: str = Field(min_length=1, max_length=80)
    display_name: str | None = None
    original_filename: str | None = None
    doc_kind: str | None = None
    text_excerpt: str = ""
    facts: list[dict] | None = None


class FactConflictPacketBody(BaseModel):
    documents: list[FactConflictPacketDoc] = Field(default_factory=list, max_length=80)


def _require_analysis_auth(
    authorization: str | None = Header(default=None),
    x_api_key: str | None = Header(default=None, alias="X-API-Key"),
) -> dict:
    keys = settings.analysis_api_key_set()
    token = None
    if authorization and authorization.lower().startswith("bearer "):
        token = authorization[7:].strip()
    elif x_api_key:
        token = x_api_key.strip()

    mode = settings.resolve_app_mode()
    # Hosted analysis: API key required (no open-entitlement bypass).
    if mode == "analysis":
        if not keys or not token or token not in keys:
            raise HTTPException(401, "Valid analysis API key required.")
        return {"auth": "api_key"}

    if keys:
        if not token or token not in keys:
            raise HTTPException(401, "Valid analysis API key required.")
        return {"auth": "api_key"}

    # Local vault: fall back to product entitlement (cloud/license/open).
    try:
        return require_scan_allowed()
    except PermissionError as exc:
        raise HTTPException(402, str(exc)) from exc


@v1.post("/jobs", response_model=UploadResponse)
async def create_ephemeral_job(
    file: UploadFile = File(...),
    submitted_by: str = Form(default="desktop-client"),
    display_name: str | None = Form(default=None),
    doc_kind: str | None = Form(default=None),
    peer_snapshots: str | None = Form(default=None),
    _auth: dict = Depends(_require_analysis_auth),
):
    ensure_sweeper()
    ensure_watchdog()
    if not file.filename:
        raise HTTPException(400, "A file name is required.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(400, f"Unsupported file type '{suffix}'.")

    peers: list[dict] = []
    if peer_snapshots and peer_snapshots.strip():
        import json

        try:
            raw = json.loads(peer_snapshots)
        except json.JSONDecodeError as exc:
            raise HTTPException(400, "peer_snapshots must be JSON.") from exc
        if not isinstance(raw, list):
            raise HTTPException(400, "peer_snapshots must be a JSON list.")
        peers = [row for row in raw[:80] if isinstance(row, dict)]

    job_id = uuid.uuid4().hex
    dest_dir = job_dir(job_id)
    safe_name = Path(file.filename).name
    dest = dest_dir / safe_name

    size = 0
    max_bytes = settings.max_file_mb * 1024 * 1024
    with dest.open("wb") as handle:
        while True:
            chunk = await file.read(1024 * 1024)
            if not chunk:
                break
            size += len(chunk)
            if size > max_bytes:
                handle.close()
                delete_job_files(job_id)
                raise HTTPException(400, f"File exceeds {settings.max_file_mb} MB limit.")
            handle.write(chunk)

    create_job(job_id, safe_name)
    update_job(job_id, message="Queued for ephemeral analysis.")
    thread = threading.Thread(
        target=_run_ephemeral,
        args=(job_id, dest, submitted_by, display_name, doc_kind, peers),
        daemon=True,
        name=f"ephemeral-{job_id[:8]}",
    )
    thread.start()
    return UploadResponse(job_id=job_id, filename=safe_name, status="queued")


def _run_ephemeral(
    job_id: str,
    dest: Path,
    submitted_by: str,
    display_name: str | None,
    doc_kind: str | None,
    prior_snapshots: list[dict] | None = None,
) -> None:
    if not acquire_scan_slot(job_id, timeout=600):
        emit_scan_failed(
            job_id=job_id,
            stage="queue",
            error_class="timeout",
            error_message="Scan queue timed out.",
        )
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message="Scan queue timed out.",
            error="Another scan held the queue for too long.",
        )
        maybe_delete_after_terminal(job_id)
        return
    try:
        run_pipeline(
            job_id,
            dest,
            submitted_by=submitted_by,
            display_name=display_name,
            doc_kind=doc_kind,
            prior_snapshots=prior_snapshots or None,
        )
    except Exception as exc:
        emit_scan_failed(job_id=job_id, stage="failed", error_message=str(exc))
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message="Analysis failed.",
            error=str(exc),
        )
    finally:
        release_scan_slot(job_id)
        maybe_delete_after_terminal(job_id)


@v1.get("/jobs/{job_id}")
def get_ephemeral_job(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    progress = load_progress(job_id)
    if progress is None:
        raise HTTPException(404, "Unknown or expired job.")
    payload = progress.model_dump(mode="json")
    payload["ephemeral"] = True
    if progress.status == "complete":
        report = load_report(job_id)
        payload["report"] = report.model_dump(mode="json") if report else None
    else:
        payload["report"] = None
    return payload


@v1.get("/jobs/{job_id}/report")
def get_ephemeral_report(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    report = load_report(job_id)
    if report is None:
        progress = load_progress(job_id)
        if progress is None:
            raise HTTPException(404, "Unknown or expired job.")
        raise HTTPException(409, "Report is not ready yet.")
    return JSONResponse(report.model_dump(mode="json"))


@v1.delete("/jobs/{job_id}")
def purge_ephemeral_job(job_id: str, _auth: dict = Depends(_require_analysis_auth)):
    ok = delete_job_files(job_id)
    if not ok:
        raise HTTPException(404, "Unknown or already purged job.")
    return {"deleted": job_id}


@v1.post("/case-chat")
def ephemeral_case_chat(
    body: CaseChatPacketBody,
    _auth: dict = Depends(_require_analysis_auth),
):
    """Answer from client-supplied excerpts only — request-scoped, no document vault."""
    docs = [item.model_dump() for item in body.documents]
    # Cap total excerpt payload.
    total = sum(len(d.get("text_excerpt") or "") for d in docs)
    if total > 600_000:
        raise HTTPException(413, "Excerpt packet too large. Rescan fewer documents or shorten excerpts.")
    try:
        result = ask_case_from_packet(
            body.question,
            docs,
            history=body.history,
            focus_document_id=body.focus_document_id,
        )
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    citations = result.get("citations") or []
    emit_chat_asked(
        used_model=bool(result.get("used_model")),
        hit_count=len(citations),
        question_len=len(body.question or ""),
    )
    return {
        "answer": result.get("answer"),
        "citations": citations,
        "used_model": bool(result.get("used_model")),
        "ephemeral": True,
    }


@v1.post("/fact-conflicts")
def ephemeral_fact_conflicts(
    body: FactConflictPacketBody,
    _auth: dict = Depends(_require_analysis_auth),
):
    """Case-level labeled-fact conflicts from a desktop report packet (no vault).

    Ephemeral /v1/jobs scans omit case_id, so occupancy_start and peers never
    enter compare_to_case at scan time. The thin client posts cached
    comparison.facts / text_excerpt here to rebuild the case board.
    """
    from app.case_consistency import fact_conflicts_from_packet

    docs = [item.model_dump() for item in body.documents]
    total = sum(len(d.get("text_excerpt") or "") for d in docs)
    if total > 600_000:
        raise HTTPException(413, "Excerpt packet too large. Rescan fewer documents or shorten excerpts.")
    if len(docs) < 2:
        return {"fact_conflicts": [], "ephemeral": True}
    conflicts = fact_conflicts_from_packet(docs)
    material = [row for row in conflicts if row.get("severity") in {"high", "medium"}]
    return {"fact_conflicts": material, "ephemeral": True}


@v1.post("/sweep")
def force_sweep(_auth: dict = Depends(_require_analysis_auth)):
    return {"removed": sweep_stale_jobs()}
