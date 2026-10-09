"""Gemini stage thread pool + local-vs-Gemini backpressure.

Local process-pool jobs may not run more than GEMINI_MAX_CONCURRENT ahead of
the Gemini stage. Jobs in local_done (source PDF waiting) and jobs actively
running Gemini both count against that cap.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from concurrent.futures import Future, ThreadPoolExecutor
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

_executor: ThreadPoolExecutor | None = None
_LOCK = threading.Lock()
_COND = threading.Condition(_LOCK)
# Jobs that finished local and have not finished Gemini (waiting or in-flight).
_AHEAD: set[str] = set()
_GEMINI_RUNNING: set[str] = set()
_ROLLING_TOTAL_S: deque[float] = deque(maxlen=24)
_ROLLING_LOCAL_S: deque[float] = deque(maxlen=24)
_ROLLING_GEMINI_S: deque[float] = deque(maxlen=24)


def _max_ahead() -> int:
    from app.config import settings

    return max(1, int(settings.gemini_max_concurrent or 4))


def ensure_gemini_executor() -> ThreadPoolExecutor:
    global _executor
    if _executor is not None:
        return _executor
    workers = _max_ahead()
    _executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="gemini-stage")
    logger.info("gemini thread pool started max_workers=%s", workers)
    return _executor


def shutdown_gemini_executor() -> None:
    global _executor
    if _executor is None:
        return
    _executor.shutdown(wait=False, cancel_futures=False)
    _executor = None
    logger.info("gemini thread pool shut down")


def ahead_count() -> int:
    with _LOCK:
        return len(_AHEAD)


def gemini_running_count() -> int:
    with _LOCK:
        return len(_GEMINI_RUNNING)


def wait_for_local_slot(*, timeout: float = 600.0) -> bool:
    """Block until starting another local job would not exceed the ahead cap."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    with _COND:
        while len(_AHEAD) >= _max_ahead():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _COND.wait(timeout=min(1.0, remaining))
        return True


def mark_ahead(job_id: str) -> None:
    with _COND:
        _AHEAD.add(job_id)
        _COND.notify_all()


def reserve_ahead(job_id: str, *, timeout: float = 600.0) -> bool:
    """Atomically wait for capacity and reserve an ahead slot through Gemini terminal."""
    deadline = time.monotonic() + max(1.0, float(timeout))
    with _COND:
        while len(_AHEAD) >= _max_ahead():
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            _COND.wait(timeout=min(1.0, remaining))
        _AHEAD.add(job_id)
        _COND.notify_all()
        return True


def clear_ahead(job_id: str) -> None:
    with _COND:
        _AHEAD.discard(job_id)
        _GEMINI_RUNNING.discard(job_id)
        _COND.notify_all()


def record_timing(timing: dict[str, Any] | None) -> None:
    if not isinstance(timing, dict):
        return
    try:
        total = float(timing.get("total_s") or 0)
        local = float(timing.get("local_s") or 0)
        gemini = float(timing.get("gemini_s") or 0)
    except (TypeError, ValueError):
        return
    with _LOCK:
        if total > 0:
            _ROLLING_TOTAL_S.append(total)
        if local > 0:
            _ROLLING_LOCAL_S.append(local)
        if gemini > 0:
            _ROLLING_GEMINI_S.append(gemini)


def rolling_averages() -> dict[str, float | None]:
    with _LOCK:
        def _avg(q: deque[float]) -> float | None:
            if not q:
                return None
            return round(sum(q) / len(q), 2)

        return {
            "avg_total_s": _avg(_ROLLING_TOTAL_S),
            "avg_local_s": _avg(_ROLLING_LOCAL_S),
            "avg_gemini_s": _avg(_ROLLING_GEMINI_S),
            "samples": len(_ROLLING_TOTAL_S),
        }


def estimate_eta_seconds(*, queue_position: int) -> int | None:
    """Rough ETA from rolling wall time and Gemini parallelism."""
    if queue_position <= 0:
        return 0
    avgs = rolling_averages()
    avg = avgs.get("avg_total_s")
    if avg is None or avg <= 0:
        # Cold start: multimodal-heavy proxy until instrumented jobs land.
        avg = 120.0
    parallel = max(1, _max_ahead())
    # Local is serial; Gemini is parallel. Wall ≈ max(local_serial, gemini/parallel).
    local_avg = avgs.get("avg_local_s") or (avg * 0.35)
    gemini_avg = avgs.get("avg_gemini_s") or (avg * 0.55)
    serial_local = queue_position * float(local_avg)
    parallel_gemini = (queue_position / parallel) * float(gemini_avg)
    return int(max(serial_local, parallel_gemini, float(avg)))


def submit_gemini_stage(payload: dict[str, Any]) -> Future:
    """Run Gemini stage in the web-process thread pool.

    Caller must already have mark_ahead(job_id) from local submit reservation.
    """
    from app.scan_job_runner import execute_gemini_stage

    pool = ensure_gemini_executor()
    job_id = str(payload.get("job_id") or "")
    mark_ahead(job_id)  # idempotent if already reserved at local submit

    def _runner() -> dict[str, Any]:
        with _LOCK:
            _GEMINI_RUNNING.add(job_id)
        try:
            return execute_gemini_stage(payload)
        finally:
            clear_ahead(job_id)

    future = pool.submit(_runner)

    def _log_done(fut: Future) -> None:
        try:
            result = fut.result()
            timing = (result or {}).get("timing") if isinstance(result, dict) else None
            record_timing(timing)
        except Exception as exc:
            logger.exception("gemini stage crashed job=%s", job_id[:8] if job_id else "?")
            clear_ahead(job_id)
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
                    message="Gemini stage crashed.",
                    error=str(exc),
                )
                _update_durable_job(
                    job_id,
                    status="failed",
                    message="Gemini stage crashed.",
                    error=str(exc),
                )
                delete_ephemeral_source(job_id)
            except Exception:
                logger.exception("could not mark gemini crash job %s failed", job_id[:8])

    future.add_done_callback(_log_done)
    return future


def local_stage_path(job_id: str) -> Path:
    from app.storage import job_dir

    return job_dir(job_id) / "local_stage.json"


def gemini_payload_path(job_id: str) -> Path:
    from app.storage import job_dir

    return job_dir(job_id) / "gemini_payload.json"


def source_present(job_id: str, dest: str | Path | None = None) -> bool:
    if dest:
        return Path(dest).is_file()
    from app.storage import job_dir

    root = job_dir(job_id)
    if not root.exists():
        return False
    skip = {
        "progress.json",
        "report.json",
        "report.html",
        "report.pdf",
        "persist.flag",
        "local_stage.json",
        "gemini_payload.json",
    }
    for path in root.iterdir():
        if path.is_file() and path.name not in skip:
            return True
    return False
