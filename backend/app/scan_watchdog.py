"""Background stale-scan reaper — fail stuck *running* jobs and release slots.

Queued jobs are never failed for age alone (concurrency 1 makes long queues expected).
"""

from __future__ import annotations

import logging
import threading
import time
from datetime import datetime, timezone

from app import cases_db
from app.config import settings
from app.product_events import emit_scan_failed, emit_scan_progress_stall
from app.scan_slots import force_release
from app.storage import TERMINAL_STATUSES, load_progress, update_job

logger = logging.getLogger("uvicorn.error")
_started = False
_lock = threading.Lock()
# Last time any job left queued / completed — for queue-idle WARNING only.
_last_queue_progress_at: float | None = None
_queue_idle_warned = False


def ensure_watchdog() -> None:
    global _started
    with _lock:
        if _started:
            return
        _started = True
        thread = threading.Thread(target=_loop, daemon=True, name="scan-watchdog")
        thread.start()


def note_queue_progress() -> None:
    """Call when a job starts running or reaches a terminal state."""
    global _last_queue_progress_at, _queue_idle_warned
    _last_queue_progress_at = time.monotonic()
    _queue_idle_warned = False


def _loop() -> None:
    tick = 0
    while True:
        try:
            reap_all_stale_jobs()
            _maybe_warn_queue_idle()
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


def _maybe_warn_queue_idle() -> None:
    """Log when the queue as a whole makes no progress — never fail waiting jobs."""
    global _queue_idle_warned
    root = settings.jobs_dir
    if not root.exists():
        return
    now = datetime.now(timezone.utc)
    idle_s = max(300, int(getattr(settings, "scan_queue_idle_warn_seconds", 1080) or 1080))
    oldest_queued_age = 0.0
    queued_n = 0
    for child in root.iterdir():
        if not child.is_dir():
            continue
        progress = load_progress(child.name)
        if progress is None or progress.status != "queued":
            continue
        queued_n += 1
        started = _aware(progress.started_at) or _aware(progress.created_at)
        if started is not None:
            oldest_queued_age = max(oldest_queued_age, (now - started).total_seconds())
    if queued_n == 0 or oldest_queued_age < idle_s:
        _queue_idle_warned = False
        return
    last = _last_queue_progress_at
    if last is not None and (time.monotonic() - last) < idle_s:
        return
    if _queue_idle_warned:
        return
    _queue_idle_warned = True
    logger.warning(
        "queue idle: %s queued job(s); oldest waiting %.0fs with no start/complete "
        "(threshold %ss). Waiting jobs are NOT failed — check local/Gemini workers.",
        queued_n,
        oldest_queued_age,
        idle_s,
    )


def _maybe_reap_job(job_id: str, now: datetime) -> bool:
    progress = load_progress(job_id)
    if progress is None:
        return False
    if progress.status in TERMINAL_STATUSES:
        return False

    # Queued: never fail for age. Concurrency 1 makes long waits expected.
    if progress.status == "queued":
        return False

    stall_s = max(60, int(settings.scan_stall_seconds))
    max_s = max(stall_s, int(settings.scan_max_seconds))

    updated = _aware(progress.updated_at)
    started = _aware(progress.started_at) or _aware(progress.created_at)
    stage = progress.stage or "unknown"

    # local_done waits for Gemini slots — silence is expected; wall-clock only.
    if progress.status == "local_done":
        if started is not None:
            wall = (now - started).total_seconds()
            gemini_cap = max(max_s, int(getattr(settings, "gemini_stage_timeout_s", 900) or 900) * 2)
            if wall >= gemini_cap:
                return _fail_job(
                    job_id,
                    stage=stage,
                    minutes_since_update=(now - updated).total_seconds() / 60.0 if updated else wall / 60.0,
                    message="Scan timed out waiting for Gemini. Re-scan this file.",
                    error=(
                        f"Job stayed in local_done longer than {gemini_cap // 60} minutes "
                        f"(last stage: {stage}). Re-scan this file."
                    ),
                )
        return False

    # Running: no-progress heartbeat (stall)
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
    # Re-check immediately — do not overwrite a terminal status (A3).
    latest = load_progress(job_id)
    if latest is not None and latest.status in TERMINAL_STATUSES:
        logger.info(
            "watchdog skip fail job=%s already terminal status=%s",
            job_id[:12],
            latest.status,
        )
        return False

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
    try:
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message=message,
            error=error,
        )
    except Exception:
        logger.exception("watchdog update_job failed job=%s", job_id[:12])
        return False

    # Confirm sticky write landed as failed (not rejected because already complete).
    after = load_progress(job_id)
    if after is not None and after.status != "failed":
        logger.info(
            "watchdog fail rejected job=%s status=%s (sticky terminal)",
            job_id[:12],
            after.status,
        )
        return False

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
    note_queue_progress()
    logger.warning("reaped stuck job %s stage=%s: %s", job_id[:12], stage, error)
    return True
