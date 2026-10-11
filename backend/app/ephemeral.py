"""Ephemeral job storage: process uploads, then delete — no document vault."""

from __future__ import annotations

import logging
import shutil
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from app.config import settings
from app.storage import job_dir, load_progress

logger = logging.getLogger(__name__)

_sweeper_started = False
_sweeper_lock = threading.Lock()


def delete_job_files(job_id: str) -> bool:
    """Remove job directory from disk. Progress/report are gone after this."""
    from app.storage import job_dir_existing

    path = job_dir_existing(job_id)
    if path is None or not path.exists():
        return False
    shutil.rmtree(path, ignore_errors=True)
    return True


def maybe_delete_after_terminal(job_id: str, *, retain_report: bool = False) -> None:
    if not settings.ephemeral_jobs:
        return
    progress = load_progress(job_id)
    if progress is None:
        delete_job_files(job_id)
        return
    if progress.status in {"complete", "failed"}:
        # Keep report available briefly for the client to fetch, then sweeper removes.
        # Immediate delete of *source* file only to shrink retention surface.
        # SOURCE_FILE_RETENTION=immediate (product rule: never keep PDFs).
        _delete_source_bytes(job_id)
        if retain_report:
            # Persist firms: facts/report live encrypted in Postgres. Do not keep
            # report.json on disk (even encrypted) past terminal — DB is source of truth.
            from app.storage import delete_disk_reports

            flag = job_dir(job_id) / "persist.flag"
            if not flag.exists():
                flag.write_text("1", encoding="utf-8")
            delete_disk_reports(job_id)


def delete_ephemeral_source(job_id: str) -> None:
    """Drop the uploaded bytes. Log the job id only — never a filename or content."""
    if not settings.ephemeral_jobs:
        return
    _delete_source_bytes(job_id)
    logger.info("deleted ephemeral source bytes job=%s", job_id)


def _delete_source_bytes(job_id: str) -> None:
    root = job_dir(job_id)
    if not root.exists():
        return
    skip = {"progress.json", "report.json", "report.html", "report.pdf", "persist.flag"}
    for path in list(root.iterdir()):
        if path.name in skip:
            continue
        if path.is_file():
            try:
                path.unlink()
            except OSError:
                pass
        elif path.is_dir() and path.name == "artifacts":
            shutil.rmtree(path, ignore_errors=True)


def sweep_stale_jobs() -> int:
    """Delete job dirs older than TTL (complete/failed or abandoned).

    Persist-flag dirs are also swept: durable reports live in Postgres, so disk
    is only a short-lived handoff (same TTL as ephemeral jobs).
    """
    removed = 0
    root = settings.jobs_dir
    if not root.exists():
        return 0
    ttl = max(1, int(settings.ephemeral_ttl_hours)) * 3600
    now = time.time()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        try:
            age = now - child.stat().st_mtime
        except OSError:
            continue
        progress = load_progress(child.name)
        terminal = progress is None or progress.status in {"complete", "failed"}
        if terminal and age >= ttl:
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
        elif age >= ttl * 2:
            # Abandoned running jobs
            shutil.rmtree(child, ignore_errors=True)
            removed += 1
    return removed


def ensure_sweeper() -> None:
    global _sweeper_started
    with _sweeper_lock:
        if _sweeper_started or not settings.ephemeral_jobs:
            return
        _sweeper_started = True

    def _loop() -> None:
        while True:
            try:
                sweep_stale_jobs()
            except Exception:
                pass
            time.sleep(900)

    threading.Thread(target=_loop, daemon=True, name="ephemeral-sweeper").start()


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()
