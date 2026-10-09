"""Process pool for local PDF/OCR — keeps the FastAPI / uvicorn process free.

CPU-heavy local work runs in a child process so /api/health can answer within
Render's 5s window. Gemini stage is scheduled in the web process thread pool
after local_done (see gemini_executor).
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
    """Queue local stage in a child process; on local_done schedule Gemini in-web."""
    from app.gemini_executor import (
        clear_ahead,
        ensure_gemini_executor,
        reserve_ahead,
        submit_gemini_stage,
    )
    from app.scan_job_runner import execute_ephemeral_job

    ensure_gemini_executor()
    job_id = str(payload.get("job_id") or "")

    # Backpressure: reserve an ahead slot from local submit through Gemini terminal.
    if not reserve_ahead(job_id, timeout=900):
        logger.error("local backpressure timeout job=%s — failing", job_id[:8])
        try:
            from app.ephemeral import delete_ephemeral_source
            from app.storage import update_job
            from app.v1_analysis import _update_durable_job

            update_job(
                job_id,
                status="failed",
                stage="failed",
                percent=100,
                message="Queue backpressure timeout.",
                error="local_backpressure_timeout",
            )
            _update_durable_job(
                job_id,
                status="failed",
                message="Queue backpressure timeout.",
                error="local_backpressure_timeout",
            )
            delete_ephemeral_source(job_id)
        except Exception:
            logger.exception("backpressure fail path error job=%s", job_id[:8])
        fut: Future = Future()
        fut.set_result({"job_id": job_id, "status": "failed"})
        return fut

    pool = ensure_scan_executor()
    future = pool.submit(execute_ephemeral_job, payload)

    def _after_local(fut: Future) -> None:
        try:
            result = fut.result()
        except Exception as exc:
            clear_ahead(job_id)
            logger.exception("scan child crashed job=%s", job_id[:8] if job_id else "?")
            if not job_id:
                return
            try:
                from app.ephemeral import delete_ephemeral_source
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
                delete_ephemeral_source(job_id)
            except Exception:
                logger.exception("could not mark crashed job %s failed", job_id[:8])
            return

        status = (result or {}).get("status")
        if status != "local_done":
            clear_ahead(job_id)
            return
        # Resume payload may have been enriched in the child (labels, peak rss).
        gemini_payload = dict(payload)
        try:
            from app.gemini_executor import gemini_payload_path
            import json

            path = gemini_payload_path(job_id)
            if path.is_file():
                gemini_payload.update(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            logger.debug("gemini payload enrich failed job=%s", job_id[:8], exc_info=True)
        if result.get("peak_rss_mb") is not None:
            gemini_payload["peak_rss_mb"] = result["peak_rss_mb"]
        submit_gemini_stage(gemini_payload)

    future.add_done_callback(_after_local)
    return future


def shutdown_scan_executor() -> None:
    global _executor
    if _executor is None:
        return
    _executor.shutdown(wait=False, cancel_futures=False)
    _executor = None
    logger.info("scan process pool shut down")
    try:
        from app.gemini_executor import shutdown_gemini_executor

        shutdown_gemini_executor()
    except Exception:
        pass
