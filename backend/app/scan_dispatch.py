"""Choose inline (today) vs worker queue (Phase 1) for ephemeral scans."""

from __future__ import annotations

import json
import logging
import shutil
from pathlib import Path
from typing import Any

from app.blob_store import get_blob_store, job_meta_key, job_source_key, matter_source_key
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


def retain_source_blob(
    *,
    firm_id: str | None,
    matter_id: str | None,
    sha256: str | None,
    source_path: Path | None = None,
    source_bytes: bytes | None = None,
    content_type: str | None = None,
) -> str | None:
    """Copy exhibit bytes to the durable matter key when retention is enabled.

    Returns the object key only after a successful put (and exists check).
    Never invents a key for a missing object.
    """
    if not settings.keep_source_in_blob:
        return None
    if not firm_id or not matter_id or not sha256:
        return None
    try:
        key = matter_source_key(firm_id, matter_id, sha256)
    except ValueError:
        return None
    store = get_blob_store()
    mime = (content_type or "").strip() or "application/pdf"
    try:
        if source_path is not None and source_path.is_file():
            with source_path.open("rb") as handle:
                store.put(key, handle, content_type=mime)
        elif source_bytes is not None:
            store.put(key, source_bytes, content_type=mime)
        else:
            return None
        if not store.exists(key):
            logger.warning("durable source put missing after write key=%s", key[:80])
            return None
    except Exception:
        logger.exception(
            "durable source retain failed firm=%s matter=%s",
            str(firm_id)[:8],
            str(matter_id)[:8],
        )
        return None
    logger.info(
        "durable source retained firm=%s matter=%s bytes=%s",
        str(firm_id)[:8],
        str(matter_id)[:8],
        (source_path.stat().st_size if source_path and source_path.is_file() else len(source_bytes or b"")),
    )
    return key


def resolve_retained_source_key(
    *,
    firm_id: str | None,
    matter_id: str | None,
    sha256: str | None,
    preferred_key: str | None = None,
) -> str | None:
    """Return a durable key only when the object actually exists in blob storage."""
    if not settings.keep_source_in_blob:
        return None
    store = get_blob_store()
    candidates: list[str] = []
    pref = (preferred_key or "").strip()
    if pref:
        candidates.append(pref)
    if firm_id and matter_id and sha256:
        try:
            candidates.append(matter_source_key(firm_id, matter_id, sha256))
        except ValueError:
            pass
    seen: set[str] = set()
    for key in candidates:
        if not key or key in seen:
            continue
        seen.add(key)
        try:
            if store.exists(key):
                return key
        except Exception:
            logger.debug("durable source exists check failed key=%s", key[:80], exc_info=True)
    return None


def _enqueue_worker_job(payload: dict[str, Any]) -> None:
    job_id = str(payload["job_id"])
    dest = Path(payload["dest"])
    if not dest.is_file():
        raise FileNotFoundError(f"missing upload for job {job_id}")

    firm_ctx = payload.get("firm_ctx") or {}
    source_name = dest.name
    sha256 = str(payload.get("sha256") or "").strip().lower()
    # Prefer a key already written at upload time; re-put if missing.
    durable_key = resolve_retained_source_key(
        firm_id=firm_ctx.get("firm_id"),
        matter_id=payload.get("matter_id"),
        sha256=sha256,
        preferred_key=payload.get("source_blob_key"),
    )
    if not durable_key:
        durable_key = retain_source_blob(
            firm_id=firm_ctx.get("firm_id"),
            matter_id=payload.get("matter_id"),
            sha256=sha256,
            source_path=dest,
        )
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
        "sha256": sha256,
        "source_blob_key": durable_key,
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
    logger.info(
        "enqueued worker job=%s backend=%s durable=%s",
        job_id[:8],
        scan_backend(),
        bool(durable_key),
    )
