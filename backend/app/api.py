from __future__ import annotations

import re
import shutil
import threading
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import FileResponse, JSONResponse

from app.config import settings
from app.models import UploadResponse
from app.pipeline import run_pipeline
from app.storage import artifact_dir, create_job, job_dir, load_progress, load_report, update_job

router = APIRouter()
SAFE_ARTIFACT = re.compile(r"^[A-Za-z0-9._\-]+$")


@router.get("/health")
def health():
    return {"status": "ok", "name": settings.app_name, "version": settings.app_version}


@router.post("/upload", response_model=UploadResponse)
async def upload(
    file: UploadFile = File(...),
    submitted_by: str = Form(default="court-staff"),
):
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

    create_job(job_id, safe_name)
    _start_analysis(job_id, dest, submitted_by)
    return UploadResponse(job_id=job_id, filename=safe_name, status="queued")


@router.post("/analyze/{job_id}", response_model=UploadResponse)
def analyze(job_id: str, submitted_by: str = Query(default="court-staff")):
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


@router.get("/report/{job_id}")
def get_report_alias(job_id: str):
    return get_report(job_id)


def _start_analysis(job_id: str, dest: Path, submitted_by: str) -> None:
    thread = threading.Thread(
        target=_run_job,
        args=(job_id, dest, submitted_by),
        daemon=True,
    )
    thread.start()


def _source_file(job_id: str) -> Path | None:
    skip = {"progress.json", "report.json", "report.html", "report.pdf"}
    for path in job_dir(job_id).iterdir():
        if path.is_file() and path.name not in skip:
            return path
    return None


def _run_job(job_id: str, dest: Path, submitted_by: str) -> None:
    try:
        run_pipeline(job_id, dest, submitted_by=submitted_by)
    except Exception as exc:
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message="Analysis failed.",
            error=str(exc),
        )


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
