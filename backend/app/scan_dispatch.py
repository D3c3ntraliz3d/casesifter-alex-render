"""Choose inline (today) vs worker queue (Phase 1) for ephemeral scans."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.blob_store import get_blob_store, job_meta_key, job_source_key
from app.config import settings
from app.job_queue import QueueMessage, get_job_queue

logger = logging.getLogger(__name__)


def scan_backend() -> str:
    return (settings.scan_backend or "inline").strip().lower()


def dispatch_ephemeral_job(payload: dict[str, Any]) -> str:
    """Schedule a job. Returns ``inline`` or ``worker``."""
    backend = scan_backend()
    if backend in {"worker", "local_worker"}:
        _enqueue_worker_job(payload)
        return "worker"
    from app.scan_executor import submit_ephemeral_job

    submit_ephemeral_job(payload)
    return "inline"


def _enqueue_worker_job(payload: dict[str, Any]) -> None:
    job_id = str(payload["job_id"])
    dest = Path(payload["dest"])
    if not dest.is_file():
        raise FileNotFoundError(f"missing upload for job {job_id}")

    firm_ctx = payload.get("firm_ctx") or {}
    source_name = dest.name
    meta = {
        "job_id": job_id,
        "firm_id": firm_ctx.get("firm_id"),
        "matter_id": payload.get("matter_id"),
        "user_id": firm_ctx.get("user_id"),
        "submitted_by": payload.get("submitted_by") or "web",
        "display_name": payload.get("display_name"),
        "doc_kind": payload.get("doc_kind"),
        "filed_date": payload.get("filed_date"),
        "prior_snapshots": payload.get("prior_snapshots") or [],
        "persist": bool(payload.get("persist")),
        "source_name": source_name,
        "attempt": 1,
        "max_attempts": int(settings.worker_max_attempts or 3),
    }
    store = get_blob_store()
    with dest.open("rb") as handle:
        store.put(job_source_key(job_id, source_name), handle, content_type="application/octet-stream")
    store.put(
        job_meta_key(job_id),
        json.dumps(meta, separators=(",", ":")).encode("utf-8"),
        content_type="application/json",
    )

    # Source bytes are in the blob store; drop the web-process copy immediately.
    try:
        dest.unlink(missing_ok=True)
    except OSError:
        pass

    get_job_queue().enqueue(
        QueueMessage(
            job_id=job_id,
            bundle_prefix=f"jobs/{job_id}",
            attempt=1,
            firm_id=meta.get("firm_id"),
            matter_id=meta.get("matter_id"),
        )
    )
    logger.info("enqueued worker job=%s backend=%s", job_id[:8], scan_backend())
