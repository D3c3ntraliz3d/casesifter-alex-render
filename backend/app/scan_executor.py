"""Local PDF/OCR via a terminable multiprocessing.Process (SCAN_MAX_CONCURRENT=1).

ProcessPoolExecutor.shutdown does not stop a running task. We run each local
stage in a Process we can terminate on SCAN_JOB_TIMEOUT_S, then start clean.
"""

from __future__ import annotations

import logging
import multiprocessing as mp
import threading
import time
from concurrent.futures import Future
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

_LOCK = threading.Lock()
_ACTIVE_PROC: mp.Process | None = None
_ACTIVE_JOB: str | None = None
_ACTIVE_KIND: str | None = None  # "scan" | "recompute"
_RESULT_QUEUE: Any = None


def local_slot_busy() -> bool:
    """True when a scan or recompute holds the exclusive local slot."""
    with _LOCK:
        if _ACTIVE_JOB is not None:
            return True
        return _ACTIVE_PROC is not None and _ACTIVE_PROC.is_alive()


def claim_local_slot(owner_id: str, *, kind: str = "scan") -> None:
    """Wait until the exclusive local slot is free, then claim it (no Process yet)."""
    global _ACTIVE_PROC, _ACTIVE_JOB, _ACTIVE_KIND
    while True:
        with _LOCK:
            proc_busy = _ACTIVE_PROC is not None and _ACTIVE_PROC.is_alive()
            claimed = _ACTIVE_JOB is not None and _ACTIVE_JOB != owner_id
            if not proc_busy and not claimed:
                _ACTIVE_JOB = owner_id
                _ACTIVE_KIND = kind
                # Keep any stale dead process reference cleared.
                if _ACTIVE_PROC is not None and not _ACTIVE_PROC.is_alive():
                    _ACTIVE_PROC = None
                return
        time.sleep(0.2)


def bind_local_process(owner_id: str, proc: mp.Process) -> None:
    global _ACTIVE_PROC
    with _LOCK:
        if _ACTIVE_JOB == owner_id:
            _ACTIVE_PROC = proc


def release_local_slot(owner_id: str) -> None:
    global _ACTIVE_PROC, _ACTIVE_JOB, _ACTIVE_KIND
    with _LOCK:
        if _ACTIVE_JOB == owner_id:
            _ACTIVE_PROC = None
            _ACTIVE_JOB = None
            _ACTIVE_KIND = None


def wait_memory_budget(job_id: str, *, reserve_gemini_ahead: bool = True) -> None:
    """Wait forever for memory budget (+ optional Gemini ahead). Never fail the job."""
    from app.gemini_executor import ahead_count, reserve_ahead
    from app.gemini_inflight import stats as inflight_stats
    from app.resource import rss_mb

    guard = _memory_guard_mb()
    while True:
        web = rss_mb()
        st = inflight_stats()
        inflight_mb = float(st.get("inflight_mb") or 0)
        max_mb = float(st.get("max_mb") or 60)
        over_inflight = inflight_mb > max_mb
        over_rss = web is not None and web >= guard
        if not over_inflight and not over_rss:
            break
        logger.warning(
            "memory guard pause local start job=%s web_rss_mb=%s guard_mb=%s "
            "inflight_mb=%s/%s (waiting — not failing)",
            job_id[:8],
            web,
            guard,
            inflight_mb,
            max_mb,
        )
        time.sleep(2.0)

    if not reserve_gemini_ahead:
        return

    while not reserve_ahead(job_id, timeout=30.0):
        web = rss_mb()
        st = inflight_stats()
        logger.warning(
            "local backpressure wait job=%s ahead=%s inflight_mb=%s web_rss_mb=%s "
            "(waiting — not failing)",
            job_id[:8],
            ahead_count(),
            st.get("inflight_mb"),
            web,
        )


def _pdf_page_count(dest: str | Path | None) -> int:
    if not dest:
        return 0
    path = Path(str(dest))
    if not path.is_file() or path.suffix.lower() != ".pdf":
        return 0
    try:
        from pypdf import PdfReader

        return max(0, len(PdfReader(str(path)).pages))
    except Exception:
        return 0


def _job_timeout_s(*, page_count: int = 0) -> float:
    from app.config import settings

    base = float(max(60, int(getattr(settings, "scan_job_timeout_base_s", 120) or 120)))
    per_page = float(max(0, int(getattr(settings, "scan_job_timeout_per_page_s", 10) or 10)))
    floor = float(max(60, int(settings.scan_job_timeout_s or 600)))
    pages = max(0, int(page_count or 0))
    scaled = base + per_page * pages
    # Prefer the larger of legacy floor and page-scaled budget so big scans get time.
    return max(floor, scaled) if pages else floor


def _memory_guard_mb() -> float:
    from app.config import settings

    return float(max(256, int(getattr(settings, "scan_memory_guard_mb", 420) or 420)))


def _wait_for_local_start(job_id: str) -> None:
    """Wait forever for memory budget + Gemini ahead slots. Never fail the job."""
    from app.gemini_inflight import stats as inflight_stats
    from app.resource import rss_mb

    wait_memory_budget(job_id, reserve_gemini_ahead=True)
    logger.info(
        "local start cleared job=%s web_rss_mb=%s inflight_mb=%s",
        job_id[:8],
        rss_mb(),
        inflight_stats().get("inflight_mb"),
    )


def _local_worker(payload: dict[str, Any], result_q: Any) -> None:
    """Child entry: run local stage and put result on queue. No writes after kill."""
    try:
        from app.scan_job_runner import execute_ephemeral_job

        result = execute_ephemeral_job(payload)
        result_q.put(("ok", result))
    except Exception as exc:
        result_q.put(("err", str(exc)))


def ensure_scan_executor() -> None:
    """Compatibility no-op — local work uses per-job Processes."""
    return None


def shutdown_scan_executor() -> None:
    global _ACTIVE_PROC, _ACTIVE_JOB, _ACTIVE_KIND
    with _LOCK:
        proc = _ACTIVE_PROC
        _ACTIVE_PROC = None
        _ACTIVE_JOB = None
        _ACTIVE_KIND = None
    if proc is not None and proc.is_alive():
        try:
            proc.terminate()
            proc.join(timeout=5)
            if proc.is_alive():
                proc.kill()
                proc.join(timeout=2)
        except Exception:
            logger.exception("failed to terminate active local process")
    try:
        from app.gemini_executor import shutdown_gemini_executor

        shutdown_gemini_executor()
    except Exception:
        pass


def _mark_local_timeout(job_id: str, stage: str | None, *, timeout_s: float) -> None:
    from app.ephemeral import delete_ephemeral_source
    from app.storage import load_progress, update_job
    from app.v1_analysis import _update_durable_job

    progress = load_progress(job_id)
    stopped = (progress.stage if progress else None) or stage or "local"
    msg = f"Local stage timed out after {timeout_s:.0f}s (last stage: {stopped})."
    err = f"local_timeout:{stopped}"
    update_job(
        job_id,
        status="failed",
        stage="failed",
        percent=100,
        message=msg,
        error=err,
    )
    _update_durable_job(job_id, status="failed", message=msg, error=err)
    try:
        from app.fact_memory.store import get_store

        row = get_store().get_analysis_job(job_id)
        attempts = int((row or {}).get("attempts") or 1)
        if attempts >= 3:
            delete_ephemeral_source(job_id)
    except Exception:
        delete_ephemeral_source(job_id)


def _maybe_retry_local(payload: dict[str, Any], job_id: str) -> bool:
    """Bump attempts and re-submit local once if under max and source present."""
    from app.fact_memory.store import get_store
    from app.gemini_executor import source_present
    from app.storage import update_job
    from app.v1_analysis import _update_durable_job

    dest = payload.get("dest")
    if not source_present(job_id, dest) and not (dest and Path(str(dest)).is_file()):
        update_job(
            job_id,
            status="interrupted",
            stage="interrupted",
            percent=0,
            message="Local timeout; source gone — re-submit from device cache.",
            error="interrupted_source_missing",
        )
        _update_durable_job(
            job_id,
            status="interrupted",
            message="Local timeout; source gone — re-submit from device cache.",
            error="interrupted_source_missing",
        )
        return False
    try:
        store = get_store()
        row = store.get_analysis_job(job_id) or {}
        attempts = int(row.get("attempts") or 1)
        if attempts >= 3:
            update_job(
                job_id,
                status="failed",
                stage="failed",
                percent=100,
                message="Needs review: local stage failed 3 times (timeout).",
                error="local_timeout_exhausted",
            )
            _update_durable_job(
                job_id,
                status="failed",
                message="Needs review: local stage failed 3 times (timeout).",
                error="local_timeout_exhausted",
            )
            return False
        store.bump_analysis_job_attempts(
            job_id,
            status="queued",
            message=f"Auto-retry local after timeout (attempt {attempts + 1}).",
        )
        update_job(
            job_id,
            status="queued",
            stage="queued",
            percent=0,
            message=f"Auto-retry local after timeout (attempt {attempts + 1}).",
        )
        threading.Thread(
            target=lambda: submit_ephemeral_job(payload),
            daemon=True,
            name=f"retry-local-{job_id[:8]}",
        ).start()
        return True
    except Exception:
        logger.exception("local retry schedule failed job=%s", job_id[:8])
        return False


def submit_ephemeral_job(payload: dict[str, Any]) -> Future:
    """Run local stage in a terminable Process; on local_done schedule Gemini."""
    from app.gemini_executor import (
        clear_ahead,
        ensure_gemini_executor,
        submit_gemini_stage,
    )
    from app.resource import rss_mb
    from app.scan_watchdog import note_queue_progress

    ensure_gemini_executor()
    job_id = str(payload.get("job_id") or "")
    out: Future = Future()

    def _run() -> None:
        global _ACTIVE_PROC, _ACTIVE_JOB
        try:
            _wait_for_local_start(job_id)
        except Exception:
            logger.exception("local start wait failed job=%s", job_id[:8])
            # Keep waiting path non-fatal: clear ahead and leave job queued.
            clear_ahead(job_id)
            if not out.done():
                out.set_result({"job_id": job_id, "status": "queued"})
            return

        pages = _pdf_page_count(payload.get("dest"))
        timeout = _job_timeout_s(page_count=pages)
        logger.info(
            "local process start job=%s pages=%s timeout_s=%.0f web_rss_mb=%s",
            job_id[:8],
            pages,
            timeout,
            rss_mb(),
        )

        ctx = mp.get_context("spawn")
        result_q = ctx.Queue()
        proc = ctx.Process(
            target=_local_worker,
            args=(payload, result_q),
            name=f"local-{job_id[:8]}",
            daemon=True,
        )
        # Exclusive local slot shared with recompute (512 MB: never overlap).
        claim_local_slot(job_id, kind="scan")
        result = None
        try:
            bind_local_process(job_id, proc)
            note_queue_progress()
            proc.start()
            proc.join(timeout=timeout)
            if proc.is_alive():
                logger.error(
                    "local stage timeout job=%s after %.0fs pages=%s — terminating process pid=%s",
                    job_id[:8],
                    timeout,
                    pages,
                    proc.pid,
                )
                try:
                    proc.terminate()
                    proc.join(timeout=5)
                    if proc.is_alive():
                        proc.kill()
                        proc.join(timeout=2)
                except Exception:
                    logger.exception("terminate failed job=%s", job_id[:8])
                try:
                    while not result_q.empty():
                        result_q.get_nowait()
                except Exception:
                    pass
                clear_ahead(job_id)
                _mark_local_timeout(job_id, "local", timeout_s=timeout)
                _maybe_retry_local(payload, job_id)
                if not out.done():
                    out.set_result({"job_id": job_id, "status": "failed", "timed_out": True})
                return

            try:
                kind, payload_or_err = result_q.get(timeout=5)
                if kind == "ok":
                    result = payload_or_err
                else:
                    raise RuntimeError(str(payload_or_err))
            except Exception as exc:
                clear_ahead(job_id)
                logger.exception("scan child crashed job=%s", job_id[:8] if job_id else "?")
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
                if not out.done():
                    out.set_exception(exc)
                return

            status = (result or {}).get("status")
            if status != "local_done":
                clear_ahead(job_id)
                if not out.done():
                    out.set_result(result)
                return

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
            logger.info(
                "local done → gemini job=%s peak_rss_mb=%s web_rss_mb=%s",
                job_id[:8],
                result.get("peak_rss_mb"),
                rss_mb(),
            )
            submit_gemini_stage(gemini_payload)
            if not out.done():
                out.set_result(result)
        finally:
            release_local_slot(job_id)

    threading.Thread(target=_run, daemon=True, name=f"local-wait-{job_id[:8]}").start()
    return out
