"""Process pool for exhibit analysis — keeps the FastAPI / uvicorn process free.

CPU-heavy PDF/OCR/Gemini work runs in a child process so /api/health can answer
within Render's 5s window even while a scan is running.

Uses spawn (not fork): uvicorn is multi-threaded; forking that can deadlock.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
from concurrent.futures import Future, ProcessPoolExecutor
from typing import Any

logger = logging.getLogger(__name__)

_executor: ProcessPoolExecutor | None = None


def _max_workers() -> int:
    from app.config import settings

    return max(1, int(settings.scan_max_concurrent or 1))


def ensure_scan_executor() -> ProcessPoolExecutor:
    global _executor
    if _executor is not None:
        return _executor
    workers = _max_workers()
    ctx = mp.get_context("spawn")
    # max_tasks_per_child=1 recycles the child after each job so RSS does not climb.
    _executor = ProcessPoolExecutor(
        max_workers=workers,
        max_tasks_per_child=1,
        mp_context=ctx,
    )
    logger.info("scan process pool started max_workers=%s context=spawn", workers)
    return _executor


def submit_ephemeral_job(payload: dict[str, Any]) -> Future:
    """Queue analysis in a child process. Returns immediately."""
    from app.scan_job_runner import execute_ephemeral_job

    pool = ensure_scan_executor()
    job_id = str(payload.get("job_id") or "")
    future = pool.submit(execute_ephemeral_job, payload)

    def _log_done(fut: Future) -> None:
        try:
            fut.result()
        except Exception as exc:
            logger.exception("scan child crashed job=%s", job_id[:8] if job_id else "?")
            if not job_id:
                return
            try:
                from app.storage import update_job
                from app.v1_analysis import _update_durable_job

                update_job(
                    job_id,
                    status="failed",
                    stage="failed",
                    percent=100,
                    message="Analysis worker crashed.",
                    error=str(exc),
                )
                _update_durable_job(
                    job_id,
                    status="failed",
                    message="Analysis worker crashed.",
                    error=str(exc),
                )
            except Exception:
                logger.exception("could not mark crashed job %s failed", job_id[:8])

    future.add_done_callback(_log_done)
    return future


def shutdown_scan_executor() -> None:
    global _executor
    if _executor is None:
        return
    _executor.shutdown(wait=False, cancel_futures=False)
    _executor = None
    logger.info("scan process pool shut down")
