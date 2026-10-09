from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.models import JobProgress, JobStatus, Report

_lock = threading.Lock()
logger = logging.getLogger("uvicorn.error")

TERMINAL_STATUSES = frozenset({"complete", "failed", "interrupted"})


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def job_dir(job_id: str) -> Path:
    path = settings.jobs_dir / job_id
    path.mkdir(parents=True, exist_ok=True)
    (path / "artifacts").mkdir(exist_ok=True)
    return path


def _progress_path(job_id: str) -> Path:
    return job_dir(job_id) / "progress.json"


def _atomic_write_text(path: Path, text: str) -> None:
    """Write via temp + fsync + os.replace so readers never see a partial JSON."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        with tmp.open("w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def save_progress(progress: JobProgress) -> None:
    progress.updated_at = utcnow()
    path = _progress_path(progress.job_id)
    # Keep the report in report.json only. Embedding it here made every status
    # poll reread a multi-megabyte file and froze the UI after large exhibits.
    slim = progress.model_copy(update={"report": None})
    with _lock:
        _atomic_write_text(path, slim.model_dump_json(indent=2))


def load_progress(job_id: str) -> JobProgress | None:
    """Load progress.json. Partial-read / parse races return None after one retry."""
    path = _progress_path(job_id)
    if not path.exists():
        return None
    last_exc: Exception | None = None
    for attempt in range(2):
        try:
            raw = path.read_text(encoding="utf-8")
            return JobProgress.model_validate_json(raw)
        except Exception as exc:
            last_exc = exc
            if attempt == 0:
                time.sleep(0.05)
                continue
    logger.warning(
        "load_progress not ready job=%s err=%s",
        job_id[:12],
        last_exc,
    )
    return None


def create_job(job_id: str, filename: str) -> JobProgress:
    progress = JobProgress(
        job_id=job_id,
        status="queued",
        stage="queued",
        percent=0,
        message="File received. Analysis will begin shortly.",
        filename=filename,
        created_at=utcnow(),
        updated_at=utcnow(),
    )
    save_progress(progress)
    return progress


def update_job(
    job_id: str,
    *,
    status: JobStatus | None = None,
    stage: str | None = None,
    percent: int | None = None,
    message: str | None = None,
    error: str | None = None,
    report: Report | None = None,
) -> JobProgress:
    """Update job progress. Terminal statuses are sticky — refuse overwrite."""
    with _lock:
        path = _progress_path(job_id)
        if not path.exists():
            raise FileNotFoundError(f"Unknown job {job_id}")
        try:
            progress = JobProgress.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception as exc:
            # One brief retry for mid-write readers racing a previous writer.
            time.sleep(0.05)
            try:
                progress = JobProgress.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                raise FileNotFoundError(f"progress.json not ready for job {job_id}") from exc
        current = progress.status
        if current in TERMINAL_STATUSES and status is not None and status != current:
            logger.info(
                "sticky status refuse job=%s current=%s attempted=%s",
                job_id[:8],
                current,
                status,
            )
            return progress
        if current in TERMINAL_STATUSES and status is None:
            if report is None and (stage is not None or error is not None or percent is not None):
                logger.debug(
                    "sticky status skip non-status mutation job=%s status=%s",
                    job_id[:8],
                    current,
                )
                return progress

        if status is not None:
            if status in {"running", "local_done"} and progress.started_at is None:
                progress.started_at = utcnow()
            progress.status = status
        if stage is not None:
            progress.stage = stage
        if percent is not None:
            progress.percent = percent
        if message is not None:
            progress.message = message
        if error is not None:
            progress.error = error
        elif status in {"queued", "running", "local_done"}:
            progress.error = None
        if report is not None:
            progress.report = report
            report_path = job_dir(job_id) / "report.json"
            _atomic_write_text(report_path, report.model_dump_json(indent=2))
        progress.updated_at = utcnow()
        slim = progress.model_copy(update={"report": None})
        _atomic_write_text(path, slim.model_dump_json(indent=2))
        return progress


def load_report(job_id: str) -> Report | None:
    path = job_dir(job_id) / "report.json"
    if path.exists():
        try:
            return Report.model_validate_json(path.read_text(encoding="utf-8"))
        except Exception:
            time.sleep(0.05)
            try:
                return Report.model_validate_json(path.read_text(encoding="utf-8"))
            except Exception:
                return None
    progress = load_progress(job_id)
    return progress.report if progress else None


def artifact_dir(job_id: str) -> Path:
    return job_dir(job_id) / "artifacts"


def save_json(path: Path, data: dict) -> None:
    _atomic_write_text(path, json.dumps(data, indent=2, default=str))
