"""User-initiated cancel for pending / running analysis jobs."""

from __future__ import annotations

import logging
from typing import Any

from app.storage import TERMINAL_STATUSES, JOB_ID_RE, load_progress, update_job

logger = logging.getLogger(__name__)

CANCEL_MESSAGE = "Scan cancelled."
CANCEL_ERROR = "cancelled_by_user"
_ACTIVE = frozenset({"queued", "running", "local_done"})


def _mark_progress_cancelled(job_id: str) -> str | None:
    """Sticky-cancel progress.json when present. Returns resulting status or None."""
    progress = load_progress(job_id)
    if progress is None:
        return None
    if progress.status in TERMINAL_STATUSES:
        return progress.status
    try:
        updated = update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            percent=100,
            message=CANCEL_MESSAGE,
            error=CANCEL_ERROR,
        )
        return updated.status
    except FileNotFoundError:
        return None


def _write_worker_cancel_flag(job_id: str) -> None:
    try:
        from app.blob_store import get_blob_store, job_cancel_key

        get_blob_store().put(job_cancel_key(job_id), b"1", content_type="text/plain")
    except Exception:
        logger.debug("cancel flag write failed job=%s", job_id[:8], exc_info=True)


def worker_cancel_requested(job_id: str) -> bool:
    """True when analysis wrote a cancel.flag for this worker bundle."""
    try:
        from app.blob_store import get_blob_store, job_cancel_key

        return bool(get_blob_store().exists(job_cancel_key(job_id)))
    except Exception:
        return False


def cancel_analysis_job(job_id: str, *, firm_id: str | None = None) -> dict[str, Any]:
    """Mark job cancelled, stop queue/local/Gemini work when safe.

    Idempotent for already-terminal jobs. ``firm_id`` is an optional defense-in-depth
    check against the durable row (caller should already have required ownership).
    """
    jid = (job_id or "").strip()
    if not JOB_ID_RE.fullmatch(jid):
        raise ValueError("invalid job_id")

    from app.fact_memory.store import get_store
    from app.gemini_cancel import request_cancel
    from app.v1_analysis import _update_durable_job

    store = get_store()
    row = store.get_analysis_job(jid) or {}
    if firm_id and row.get("firm_id") and row.get("firm_id") != firm_id:
        raise PermissionError("firm mismatch")

    prior = str(row.get("status") or "")
    progress = load_progress(jid)
    progress_status = progress.status if progress else None

    # Already done — do not reopen.
    if prior in TERMINAL_STATUSES or (progress_status and progress_status in TERMINAL_STATUSES):
        status = progress_status if progress_status in TERMINAL_STATUSES else prior
        return {
            "job_id": jid,
            "status": status,
            "cancelled": status == "cancelled",
            "already_terminal": True,
        }

    # Signal cooperative cancel before sticky status so in-flight Gemini sees it.
    request_cancel(jid)
    _write_worker_cancel_flag(jid)

    try:
        from app.gemini_executor import clear_ahead

        clear_ahead(jid)
    except Exception:
        logger.debug("clear_ahead on cancel failed job=%s", jid[:8], exc_info=True)

    try:
        from app.scan_executor import terminate_active_local_job

        if terminate_active_local_job(jid):
            logger.info("cancelled local process job=%s", jid[:8])
    except Exception:
        logger.debug("local terminate on cancel failed job=%s", jid[:8], exc_info=True)

    dropped = False
    try:
        from app.job_queue import get_job_queue

        dropped = bool(get_job_queue().drop_ready(jid))
        if dropped:
            logger.info("dropped ready queue job=%s", jid[:8])
    except Exception:
        logger.debug("queue drop on cancel failed job=%s", jid[:8], exc_info=True)

    progress_result = _mark_progress_cancelled(jid)
    _update_durable_job(
        jid,
        status="cancelled",
        message=CANCEL_MESSAGE,
        error=CANCEL_ERROR,
    )
    after = store.get_analysis_job(jid) or {}
    final_status = (
        progress_result
        or str(after.get("status") or "")
        or "cancelled"
    )

    # Only purge the worker bundle when we actually removed it from ready.
    # In-flight claims keep the cancel.flag so the worker can exit cleanly.
    if dropped:
        try:
            from app.blob_store import get_blob_store, job_bundle_prefix

            get_blob_store().delete_prefix(job_bundle_prefix(jid))
        except Exception:
            logger.debug("bundle delete on cancel failed job=%s", jid[:8], exc_info=True)
    try:
        from app.ephemeral import delete_ephemeral_source

        # Safe while Gemini/local still run — they already staged what they need,
        # or will see the cancel flag / sticky cancelled status.
        delete_ephemeral_source(jid)
    except Exception:
        logger.debug("ephemeral cleanup on cancel failed job=%s", jid[:8], exc_info=True)

    logger.info(
        "job cancel job=%s prior=%s final=%s dropped_ready=%s",
        jid[:8],
        prior or progress_status or "?",
        final_status,
        dropped,
    )
    return {
        "job_id": jid,
        "status": final_status,
        "cancelled": final_status == "cancelled",
        "already_terminal": final_status != "cancelled" and final_status in TERMINAL_STATUSES,
        "dropped_ready": dropped,
    }
