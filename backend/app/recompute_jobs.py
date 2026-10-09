"""In-process async recompute-facts jobs (not on the local scan slot)."""

from __future__ import annotations

import logging
import threading
import time
import uuid
from typing import Any, Callable

logger = logging.getLogger("uvicorn.error")

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}
# One recompute at a time per process — cheap and avoids thrashing Postgres.
_WORKER_LOCK = threading.Lock()


def _utcnow() -> float:
    return time.time()


def get_recompute_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _JOBS.get(job_id)
        return dict(row) if row else None


def _patch(job_id: str, **fields: Any) -> None:
    with _LOCK:
        row = _JOBS.get(job_id)
        if not row:
            return
        row.update(fields)
        row["updated_at"] = _utcnow()


def start_recompute_job(
    *,
    firm_id: str,
    matter_id: str,
    user_id: str | None,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = _utcnow()
    row = {
        "job_id": job_id,
        "firm_id": firm_id,
        "matter_id": matter_id,
        "status": "queued",
        "percent": 0,
        "message": "Queued",
        "updated": 0,
        "total": 0,
        "skipped_no_excerpt": 0,
        "before": None,
        "after": None,
        "error": None,
        "created_at": now,
        "updated_at": now,
        "started_at": None,
        "finished_at": None,
    }
    with _LOCK:
        _JOBS[job_id] = row

    def _run() -> None:
        with _WORKER_LOCK:
            _patch(job_id, status="running", started_at=_utcnow(), message="Starting")
            try:
                from app.fact_memory.store import get_store

                store = get_store()

                def on_progress(
                    *,
                    done: int,
                    total: int,
                    updated: int,
                    skipped: int,
                    phase: str,
                ) -> None:
                    pct = 5
                    if total > 0 and phase == "docs":
                        pct = min(90, 5 + int(85 * done / total))
                    elif phase == "aggregate":
                        pct = 95
                    _patch(
                        job_id,
                        status="running",
                        percent=pct,
                        updated=updated,
                        total=total,
                        skipped_no_excerpt=skipped,
                        message=(
                            f"Re-extracting facts ({done}/{total})"
                            if phase == "docs"
                            else "Refreshing case-impact cards"
                        ),
                    )

                result = store.recompute_facts_from_excerpts(
                    firm_id,
                    matter_id,
                    user_id=user_id,
                    progress=on_progress,
                )
                _patch(
                    job_id,
                    status="complete",
                    percent=100,
                    message="Done",
                    updated=int(result.get("updated") or 0),
                    skipped_no_excerpt=int(result.get("skipped_no_excerpt") or 0),
                    before=result.get("before") or [],
                    after=result.get("after") or [],
                    finished_at=_utcnow(),
                )
            except KeyError:
                _patch(
                    job_id,
                    status="failed",
                    percent=100,
                    message="Matter not found",
                    error="matter_not_found",
                    finished_at=_utcnow(),
                )
            except Exception as exc:
                logger.exception("recompute job failed job=%s matter=%s", job_id[:8], matter_id)
                _patch(
                    job_id,
                    status="failed",
                    percent=100,
                    message=f"{exc.__class__.__name__}: {exc}",
                    error=str(exc)[:500],
                    finished_at=_utcnow(),
                )

    threading.Thread(
        target=_run, daemon=True, name=f"recompute-{job_id[:8]}"
    ).start()
    return dict(row)


def wait_recompute_idle(timeout_s: float = 0.1) -> bool:
    """Test helper: True if worker lock is free within timeout."""
    ok = _WORKER_LOCK.acquire(timeout=timeout_s)
    if ok:
        _WORKER_LOCK.release()
    return ok
