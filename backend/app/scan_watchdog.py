"""Background stale-scan reaper — fail stuck jobs and release scan slots."""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app import cases_db
from app.config import settings
from app.product_events import emit_scan_failed, emit_scan_progress_stall
from app.scan_slots import force_release
from app.storage import load_progress, update_job

logger = logging.getLogger(__name__)
_started = False
_lock = threading.Lock()


def ensure_watchdog() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(target=_loop, daemon=True, name="scan-watchdog")
        thread.start()


def _loop() -> None:
    # Case-doc sweep is heavier (SQLite); run less often than job-dir checks.
    tick = 0
    while True:
        try:
            reap_all_stale_jobs()
            tick += 1
            if tick % 5 == 0:
                _reap_case_documents()
        except Exception:
            logger.exception("scan watchdog tick failed")
        time.sleep(30)


def reap_all_stale_jobs() -> int:
    root = settings.jobs_dir
    if not root.exists():
        return 0
    reaped = 0
    now = datetime.now(timezone.utc)
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if _maybe_reap_job(child.name, now):
            reaped += 1
    return reaped


def _reap_case_documents() -> None:
    for case in cases_db.list_cases():
        case_id = case.get("id")
        if not case_id:
            continue
        for doc in cases_db.list_documents(case_id):
            if doc.get("status") not in {"scanning", "queued"}:
                continue
            job_id = doc.get("scan_job_id")
            if job_id and _maybe_reap_job(job_id, datetime.now(timezone.utc)):
                cases_db.update_document(doc["id"], status="failed")


def _aware(dt: datetime | None) -> datetime | None:
    if dt is None:
        return None
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt


def _maybe_reap_job(job_id: str, now: datetime) -> bool:
    progress = load_progress(job_id)
    if progress is None:
        return False
    if progress.status in {"complete", "failed", "interrupted"}:
        return False

    stall_s = max(60, int(settings.scan_stall_seconds))
    max_s = max(stall_s, int(settings.scan_max_seconds))

    updated = _aware(progress.updated_at)
    started = _aware(progress.started_at) or _aware(progress.created_at)
    stage = progress.stage or "unknown"

    # Queued jobs are waiting for a slot — only fail them if they sat forever
    # with no start (slot holders likely hung). Use the absolute max as a ceiling.
    if progress.status == "queued":
        if started is None:
            return False
        queue_age = (now - started).total_seconds()
        if queue_age < max_s:
            return False
        return _fail_job(
            job_id,
            stage=stage,
            minutes_since_update=queue_age / 60.0,
            message="Scan never started. Re-scan this file.",
            error=(
                f"Job stayed queued for more than {max_s // 60} minutes "
                "(scan slots were likely wedged). Re-scan after the API recovers."
            ),
        )

    # local_done waits for Gemini slots — silence is expected; wall-clock only.
    if progress.status == "local_done":
        if started is not None:
            wall = (now - started).total_seconds()
            if wall >= max_s:
                return _fail_job(
                    job_id,
                    stage=stage,
                    minutes_since_update=(now - updated).total_seconds() / 60.0 if updated else wall / 60.0,
                    message="Scan timed out waiting for Gemini. Re-scan this file.",
                    error=(
                        f"Job stayed in local_done longer than {max_s // 60} minutes "
                        f"(last stage: {stage}). Re-scan this file."
                    ),
                )
        return False

    # Running: no-progress heartbeat
    if updated is not None:
        silent = (now - updated).total_seconds()
        if silent >= stall_s:
            return _fail_job(
                job_id,
                stage=stage,
                minutes_since_update=silent / 60.0,
                message="Scan stalled. Re-scan this file.",
                error=(
                    f"No progress for more than {stall_s // 60} minutes "
                    f"(last stage: {stage}). Large exhibits can hang OCR or image review; try Re-scan."
                ),
            )

    # Running: absolute wall-clock from first "running" tick
    if started is not None:
        wall = (now - started).total_seconds()
        if wall >= max_s:
            return _fail_job(
                job_id,
                stage=stage,
                minutes_since_update=(now - updated).total_seconds() / 60.0 if updated else wall / 60.0,
                message="Scan timed out. Re-scan this file.",
                error=(
                    f"Scan ran longer than {max_s // 60} minutes without finishing "
                    f"(last stage: {stage}). Re-scan this file."
                ),
            )

    return False


def _fail_job(
    job_id: str,
    *,
    stage: str,
    minutes_since_update: float,
    message: str,
    error: str,
) -> bool:
    emit_scan_progress_stall(
        job_id=job_id,
        stage=stage,
        minutes_since_update=minutes_since_update,
    )
    emit_scan_failed(
        job_id=job_id,
        stage=stage,
        error_class="timeout",
        error_message=error,
    )
    update_job(
        job_id,
        status="failed",
        stage="failed",
        percent=100,
        message=message,
        error=error,
    )
    force_release(job_id)
    try:
        from app.ephemeral import delete_ephemeral_source
        from app.gemini_executor import clear_ahead
        from app.v1_analysis import _update_durable_job

        clear_ahead(job_id)
        _update_durable_job(job_id, status="failed", message=message, error=error)
        delete_ephemeral_source(job_id)
    except Exception:
        logger.exception("watchdog cleanup failed job=%s", job_id[:12])
    logger.warning("reaped stuck job %s stage=%s: %s", job_id[:12], stage, error)
    return True
