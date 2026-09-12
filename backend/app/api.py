from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app import cases_db
from app.auto_title import propose_document_title
from app.config import settings
from app.entitlement import (
    cloud_login,
    cloud_logout,
    entitlement_status,
    require_scan_allowed,
    save_license_key,
    set_share_outcomes,
)
from app.ingestion import sha256_file
from app.models import (
    CaseCreate,
    CaseUpdate,
    DocumentUpdate,
    FindingFeedbackBody,
    LicenseBody,
    LoginBody,
    ShareOutcomesBody,
    UploadResponse,
)
from app.pipeline import run_pipeline
from app.storage import artifact_dir, create_job, job_dir, load_progress, load_report, update_job
from app.telemetry import flush_to_cloud, record_finding_feedback

router = APIRouter()
SAFE_ARTIFACT = re.compile(r"^[A-Za-z0-9._\-]+$")
SAFE_ID = re.compile(r"^[a-f0-9]{16,64}$")


@router.get("/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": settings.app_version}


# --- Entitlement / hybrid billing stubs -------------------------------------------------


@router.get("/entitlement")
def get_entitlement():
    return entitlement_status()


@router.post("/entitlement/license")
def post_license(body: LicenseBody):
    return save_license_key(body.license_key)


@router.post("/auth/login")
def post_login(body: LoginBody):
    try:
        return cloud_login(body.email, body.password)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    except RuntimeError as exc:
        raise HTTPException(502, str(exc)) from exc


@router.post("/auth/logout")
def post_logout():
    return cloud_logout()


@router.post("/settings/share-outcomes")
def post_share_outcomes(body: ShareOutcomesBody):
    return set_share_outcomes(body.enabled)


@router.post("/telemetry/finding-feedback")
def post_finding_feedback(body: FindingFeedbackBody):
    event_id = record_finding_feedback(
        finding_id=body.finding_id,
        useful=body.useful,
        category=body.category,
        field=body.field,
        doc_kind=body.doc_kind,
    )
    return {"event_id": event_id, "recorded": True}


@router.post("/telemetry/flush")
def post_telemetry_flush():
    return flush_to_cloud()


# --- Legacy single-file upload (still supported) ---------------------------------------


@router.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    submitted_by: str = Form(default="court-staff"),
):
    _ensure_entitled()
    if not file.filename:
        raise HTTPException(400, "A file name is required.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(settings.allowed_extensions))}",
        )

    job_id = uuid.uuid4().hex
    dest_dir = job_dir(job_id)
    safe_name = Path(file.filename).name
    dest = dest_dir / safe_name
    await _write_upload(file, dest)

    create_job(job_id, safe_name)
    _start_analysis(job_id, dest, submitted_by)
    return UploadResponse(job_id=job_id, filename=safe_name, status="queued")


@router.post("/analyze/{job_id}", response_model=UploadResponse)
def analyze(job_id: str, submitted_by: str = Query(default="court-staff")):
    _ensure_entitled()
    progress = load_progress(job_id)
    if progress is None:
        raise HTTPException(404, "Unknown job. Upload a file first.")
    if progress.status == "running":
        raise HTTPException(409, "Analysis is already running for this exhibit.")
    source = _source_file(job_id)
    if source is None:
        raise HTTPException(404, "No exhibit file is stored for this job.")
    update_job(
        job_id,
        status="queued",
        stage="queued",
        percent=0,
        message="Re-analysis queued.",
        error=None,
    )
    _start_analysis(job_id, source, submitted_by)
    return UploadResponse(job_id=job_id, filename=progress.filename, status="queued")


# --- Cases -----------------------------------------------------------------------------


@router.get("/cases")
def api_list_cases():
    return {"cases": cases_db.list_cases()}


@router.post("/cases")
def api_create_case(body: CaseCreate):
    case = cases_db.create_case(
        caption=body.caption,
        court=body.court,
        index_number=body.index_number,
        notes=body.notes,
    )
    return case


@router.get("/cases/{case_id}")
def api_get_case(case_id: str):
    _validate_id(case_id)
    case = cases_db.get_case(case_id)
    if case is None:
        raise HTTPException(404, "Unknown case.")
    docs = cases_db.list_documents(case_id)
    return {**case, "documents": docs}


@router.patch("/cases/{case_id}")
def api_patch_case(case_id: str, body: CaseUpdate):
    _validate_id(case_id)
    case = cases_db.update_case(
        case_id,
        caption=body.caption,
        court=body.court,
        index_number=body.index_number,
        notes=body.notes,
    )
    if case is None:
        raise HTTPException(404, "Unknown case.")
    return case


@router.post("/cases/{case_id}/archive")
def api_archive_case(case_id: str):
    _validate_id(case_id)
    case = cases_db.set_archived(case_id, True)
    if case is None:
        raise HTTPException(404, "Unknown case.")
    return case


@router.post("/cases/{case_id}/unarchive")
def api_unarchive_case(case_id: str):
    _validate_id(case_id)
    case = cases_db.set_archived(case_id, False)
    if case is None:
        raise HTTPException(404, "Unknown case.")
    return case


@router.delete("/cases/{case_id}")
def api_delete_case(case_id: str):
    _validate_id(case_id)
    if not cases_db.delete_case(case_id):
        raise HTTPException(404, "Unknown case.")
    return {"deleted": case_id}


@router.post("/cases/{case_id}/documents")
async def api_upload_document(
    case_id: str,
    file: UploadFile = File(...),
    display_name: str | None = Form(default=None),
    doc_kind: str | None = Form(default=None),
    auto_scan: str = Form(default="true"),
    submitted_by: str = Form(default="court-staff"),
):
    _ensure_entitled()
    _validate_id(case_id)
    case = cases_db.get_case(case_id)
    if case is None:
        raise HTTPException(404, "Unknown case.")
    if not file.filename:
        raise HTTPException(400, "A file name is required.")
    suffix = Path(file.filename).suffix.lower()
    if suffix not in settings.allowed_extensions:
        raise HTTPException(
            400,
            f"Unsupported file type '{suffix}'. Allowed: {', '.join(sorted(settings.allowed_extensions))}",
        )

    safe_name = Path(file.filename).name
    folder = cases_db.case_dir(case_id) / "documents"
    stored_name = f"{uuid.uuid4().hex[:10]}_{safe_name}"
    dest = folder / stored_name
    await _write_upload(file, dest)
    digest = sha256_file(dest)

    # Lightweight text peek for auto-title (PDF first page / plain text).
    sample = _peek_text(dest)
    proposed_name, proposed_kind = propose_document_title(safe_name, sample)
    final_name = (display_name or "").strip() or proposed_name
    final_kind = (doc_kind or "").strip() or proposed_kind
    should_scan = str(auto_scan).strip().lower() in {"1", "true", "yes", "on"}

    doc = cases_db.add_document(
        case_id,
        original_filename=safe_name,
        display_name=final_name,
        doc_kind=final_kind,
        stored_path=str(dest),
        sha256=digest,
    )

    job_id = None
    status = doc["status"]
    if should_scan:
        job_id = uuid.uuid4().hex
        # Copy into job dir so existing artifact/report paths keep working.
        job_path = job_dir(job_id) / safe_name
        shutil.copy2(dest, job_path)
        create_job(job_id, safe_name)
        cases_db.update_document(doc["id"], scan_job_id=job_id, status="queued")
        _start_analysis(
            job_id,
            job_path,
            submitted_by,
            case_id=case_id,
            document_id=doc["id"],
            display_name=final_name,
            doc_kind=final_kind,
        )
        status = "queued"
        doc = cases_db.get_document(doc["id"])

    return {
        "document": doc,
        "job_id": job_id,
        "status": status,
        "proposed_display_name": proposed_name,
        "proposed_doc_kind": proposed_kind,
    }


@router.patch("/cases/{case_id}/documents/{document_id}")
def api_patch_document(case_id: str, document_id: str, body: DocumentUpdate):
    _validate_id(case_id)
    _validate_id(document_id)
    doc = cases_db.get_document(document_id)
    if doc is None or doc["case_id"] != case_id:
        raise HTTPException(404, "Unknown document.")
    updated = cases_db.update_document(
        document_id,
        display_name=body.display_name,
        doc_kind=body.doc_kind,
    )
    return updated


@router.post("/cases/{case_id}/documents/{document_id}/scan", response_model=UploadResponse)
def api_scan_document(
    case_id: str,
    document_id: str,
    submitted_by: str = Query(default="court-staff"),
):
    _ensure_entitled()
    _validate_id(case_id)
    _validate_id(document_id)
    doc = cases_db.get_document(document_id)
    if doc is None or doc["case_id"] != case_id:
        raise HTTPException(404, "Unknown document.")
    if doc.get("status") == "scanning":
        raise HTTPException(409, "Scan already running for this document.")

    source = Path(doc["stored_path"])
    if not source.exists():
        raise HTTPException(404, "Stored file is missing on disk.")

    job_id = uuid.uuid4().hex
    job_path = job_dir(job_id) / Path(doc["original_filename"]).name
    shutil.copy2(source, job_path)
    create_job(job_id, job_path.name)
    cases_db.update_document(document_id, scan_job_id=job_id, status="queued")
    _start_analysis(
        job_id,
        job_path,
        submitted_by,
        case_id=case_id,
        document_id=document_id,
        display_name=doc.get("display_name"),
        doc_kind=doc.get("doc_kind"),
    )
    return UploadResponse(job_id=job_id, filename=job_path.name, status="queued")


@router.get("/cases/{case_id}/documents/{document_id}")
def api_get_document(case_id: str, document_id: str):
    _validate_id(case_id)
    _validate_id(document_id)
    doc = cases_db.get_document(document_id)
    if doc is None or doc["case_id"] != case_id:
        raise HTTPException(404, "Unknown document.")
    snap = cases_db.get_scan_snapshot(document_id)
    progress = load_progress(doc["scan_job_id"]) if doc.get("scan_job_id") else None
    return {
        "document": doc,
        "snapshot": snap,
        "job": progress.model_dump(mode="json") if progress else None,
    }


@router.get("/cases/{case_id}/documents/{document_id}/file")
def api_document_file(case_id: str, document_id: str):
    """Serve the original uploaded filing for in-app review beside the report."""
    _validate_id(case_id)
    _validate_id(document_id)
    doc = cases_db.get_document(document_id)
    if doc is None or doc["case_id"] != case_id:
        raise HTTPException(404, "Unknown document.")
    path = Path(doc["stored_path"])
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Stored file is missing on disk.")
    media = _media_type_for(path)
    return FileResponse(
        path,
        media_type=media,
        filename=doc.get("original_filename") or path.name,
        content_disposition_type="inline",
    )


@router.get("/jobs/{job_id}/source")
def api_job_source(job_id: str):
    """Serve the exhibit file stored with a scan job (fallback when not in a case)."""
    source = _source_file(job_id)
    if source is None:
        raise HTTPException(404, "No source file for this job.")
    return FileResponse(
        source,
        media_type=_media_type_for(source),
        filename=source.name,
        content_disposition_type="inline",
    )


def _media_type_for(path: Path) -> str:
    suffix = path.suffix.lower()
    return {
        ".pdf": "application/pdf",
        ".txt": "text/plain; charset=utf-8",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        ".xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
    }.get(suffix, "application/octet-stream")


@router.get("/report/{job_id}")
def get_report_alias(job_id: str):
    return get_report(job_id)


def _ensure_entitled() -> None:
    try:
        require_scan_allowed()
    except PermissionError as exc:
        raise HTTPException(402, str(exc)) from exc


def _validate_id(value: str) -> None:
    if not SAFE_ID.match(value):
        raise HTTPException(400, "Invalid id.")


def _start_analysis(
    job_id: str,
    dest: Path,
    submitted_by: str,
    *,
    case_id: str | None = None,
    document_id: str | None = None,
    display_name: str | None = None,
    doc_kind: str | None = None,
) -> None:
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, dest, submitted_by, case_id, document_id, display_name, doc_kind),
        daemon=True,
    )
    thread.start()


def _source_file(job_id: str) -> Path | None:
    skip = {"progress.json", "report.json", "report.html", "report.pdf"}
    for path in job_dir(job_id).iterdir():
        if path.is_file() and path.name not in skip:
            return path
    return None


def _run_job(
    job_id: str,
    dest: Path,
    submitted_by: str,
    case_id: str | None,
    document_id: str | None,
    display_name: str | None,
    doc_kind: str | None,
) -> None:
    try:
        run_pipeline(
            job_id,
            dest,
            submitted_by=submitted_by,
            case_id=case_id,
            document_id=document_id,
            display_name=display_name,
            doc_kind=doc_kind,
        )
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message="Analysis failed.",
            error=str(exc),
        )
        if document_id:
            cases_db.update_document(document_id, status="failed")


async def _write_upload(file: UploadFile, dest: Path) -> None:
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
                dest.unlink(missing_ok=True)
                raise HTTPException(413, f"File exceeds the {settings.max_file_mb} MB limit.")
            handle.write(chunk)


def _peek_text(path: Path, limit: int = 2500) -> str:
    suffix = path.suffix.lower()
    try:
        if suffix == ".txt":
            return path.read_text(encoding="utf-8", errors="ignore")[:limit]
        if suffix == ".pdf":
            from app.ingestion import open_pdf_reader

            reader = open_pdf_reader(path)
            if not reader.pages:
                return ""
            return (reader.pages[0].extract_text() or "")[:limit]
    except Exception:
        return ""
    return ""


@router.get("/jobs/{job_id}")
def get_job(job_id: str):
    progress = load_progress(job_id)
    if progress is None:
        raise HTTPException(404, "Unknown job.")
    payload = progress.model_dump(mode="json")
    if progress.status != "complete":
        payload["report"] = None
    return payload


@router.get("/jobs/{job_id}/report")
def get_report(job_id: str):
    report = load_report(job_id)
    if report is None:
        progress = load_progress(job_id)
        if progress is None:
            raise HTTPException(404, "Unknown job.")
        raise HTTPException(409, "Report is not ready yet.")
    return JSONResponse(report.model_dump(mode="json"))


@router.get("/jobs/{job_id}/export/json")
def export_json(job_id: str):
    path = job_dir(job_id) / "report.json"
    if not path.exists():
        raise HTTPException(404, "Report JSON is not available.")
    return FileResponse(path, media_type="application/json", filename=f"exhibit-report-{job_id}.json")


@router.get("/jobs/{job_id}/export/html")
def export_html(job_id: str):
    path = job_dir(job_id) / "report.html"
    if not path.exists():
        raise HTTPException(404, "Report HTML is not available.")
    return FileResponse(path, media_type="text/html", filename=f"exhibit-report-{job_id}.html")


@router.get("/jobs/{job_id}/export/pdf")
def export_pdf(job_id: str):
    path = job_dir(job_id) / "report.pdf"
    if not path.exists():
        raise HTTPException(404, "Report PDF is not available.")
    return FileResponse(path, media_type="application/pdf", filename=f"exhibit-report-{job_id}.pdf")


@router.get("/jobs/{job_id}/artifacts/{name}")
def get_artifact(job_id: str, name: str):
    if not SAFE_ARTIFACT.match(name):
        raise HTTPException(400, "Invalid artifact name.")
    path = artifact_dir(job_id) / name
    if not path.exists() or not path.is_file():
        raise HTTPException(404, "Artifact not found.")
    media = "image/png" if name.lower().endswith(".png") else "application/octet-stream"
    return FileResponse(path, media_type=media)


@router.delete("/jobs/{job_id}")
def delete_job(job_id: str):
    path = settings.jobs_dir / job_id
    if not path.exists():
        raise HTTPException(404, "Unknown job.")
    shutil.rmtree(path, ignore_errors=True)
    return {"deleted": job_id}
