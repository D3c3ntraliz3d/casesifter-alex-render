"""Async recompute-facts: separate Process, queued behind the local scan slot.

CPU work must not run on the web worker (GIL would starve /api/health on 512 MB).
"""

from __future__ import annotations

import json
import logging
import multiprocessing as mp
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any

logger = logging.getLogger("uvicorn.error")

_LOCK = threading.Lock()
_JOBS: dict[str, dict[str, Any]] = {}


def _utcnow() -> float:
    return time.time()


def _progress_path(job_id: str) -> Path:
    from app.config import settings

    root = Path(settings.jobs_dir) / "_recompute"
    root.mkdir(parents=True, exist_ok=True)
    return root / f"{job_id}.json"


def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    try:
        tmp.write_text(json.dumps(payload), encoding="utf-8")
        os.replace(tmp, path)
    finally:
        if tmp.exists():
            try:
                tmp.unlink()
            except OSError:
                pass


def get_recompute_job(job_id: str) -> dict[str, Any] | None:
    with _LOCK:
        row = _JOBS.get(job_id)
        if row is None:
            return None
        out = dict(row)
    # Prefer on-disk progress from the child process when present.
    path = _progress_path(job_id)
    if path.is_file():
        try:
            disk = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(disk, dict):
                out.update(disk)
                out["job_id"] = job_id
        except Exception:
            pass
    return out


def _patch(job_id: str, **fields: Any) -> None:
    with _LOCK:
        row = _JOBS.get(job_id)
        if not row:
            return
        row.update(fields)
        row["updated_at"] = _utcnow()
        snap = dict(row)
    try:
        _atomic_write_json(_progress_path(job_id), snap)
    except Exception:
        logger.debug("recompute progress write failed job=%s", job_id[:8], exc_info=True)


def _recompute_process_main(payload: dict[str, Any]) -> None:
    """Child entry — import-heavy work stays off the web process GIL."""
    job_id = str(payload.get("job_id") or "")
    progress_path = Path(str(payload.get("progress_path") or ""))
    firm_id = str(payload.get("firm_id") or "")
    matter_id = str(payload.get("matter_id") or "")
    user_id = payload.get("user_id")

    def write_progress(**fields: Any) -> None:
        body = {
            "job_id": job_id,
            "firm_id": firm_id,
            "matter_id": matter_id,
            "status": "running",
            "updated_at": time.time(),
            **fields,
        }
        try:
            _atomic_write_json(progress_path, body)
        except Exception:
            pass

    try:
        from app.fact_memory.store import FactMemoryStore, reset_store

        # Fresh store in the child (spawn-safe).
        reset_store()
        store = FactMemoryStore()

        write_progress(percent=1, message="Starting recompute", updated=0, total=0)

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
            write_progress(
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
        write_progress(
            status="complete",
            percent=100,
            message="Done",
            updated=int(result.get("updated") or 0),
            skipped_no_excerpt=int(result.get("skipped_no_excerpt") or 0),
            before=result.get("before") or [],
            after=result.get("after") or [],
            finished_at=time.time(),
        )
    except KeyError:
        write_progress(
            status="failed",
            percent=100,
            message="Matter not found",
            error="matter_not_found",
            finished_at=time.time(),
        )
    except Exception as exc:
        write_progress(
            status="failed",
            percent=100,
            message=f"{exc.__class__.__name__}: {exc}",
            error=str(exc)[:500],
            finished_at=time.time(),
        )


def start_recompute_job(
    *,
    firm_id: str,
    matter_id: str,
    user_id: str | None,
) -> dict[str, Any]:
    job_id = str(uuid.uuid4())
    now = _utcnow()
    progress_path = _progress_path(job_id)
    row = {
        "job_id": job_id,
        "firm_id": firm_id,
        "matter_id": matter_id,
        "status": "queued",
        "percent": 0,
        "message": "Queued behind scan slot",
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
    _atomic_write_json(progress_path, row)

    payload = {
        "job_id": job_id,
        "firm_id": firm_id,
        "matter_id": matter_id,
        "user_id": user_id,
        "progress_path": str(progress_path),
    }

    def _coordinator() -> None:
        from app.scan_executor import (
            bind_local_process,
            claim_local_slot,
            release_local_slot,
            wait_memory_budget,
        )

        _patch(job_id, status="queued", message="Waiting for memory / scan slot")
        try:
            # Do not overlap Gemini-heavy web RSS or an active local scan.
            wait_memory_budget(job_id, reserve_gemini_ahead=False)
            claim_local_slot(job_id, kind="recompute")
        except Exception:
            logger.exception("recompute slot wait failed job=%s", job_id[:8])
            _patch(
                job_id,
                status="failed",
                percent=100,
                message="Could not acquire local slot",
                error="slot_wait_failed",
                finished_at=_utcnow(),
            )
            return

        ctx = mp.get_context("spawn")
        proc = ctx.Process(
            target=_recompute_process_main,
            args=(payload,),
            name=f"recompute-{job_id[:8]}",
            daemon=True,
        )
        try:
            _patch(
                job_id,
                status="running",
                started_at=_utcnow(),
                message="Recompute process starting",
            )
            bind_local_process(job_id, proc)
            proc.start()
            logger.info("recompute process start job=%s pid=%s", job_id[:8], proc.pid)
            # Parent only polls the progress file — no extract_facts / GIL work.
            while proc.is_alive():
                disk = get_recompute_job(job_id) or {}
                with _LOCK:
                    if job_id in _JOBS:
                        for key in (
                            "status",
                            "percent",
                            "message",
                            "updated",
                            "total",
                            "skipped_no_excerpt",
                            "before",
                            "after",
                            "error",
                        ):
                            if key in disk and disk[key] is not None:
                                _JOBS[job_id][key] = disk[key]
                        _JOBS[job_id]["updated_at"] = _utcnow()
                proc.join(timeout=0.5)
            proc.join(timeout=5)
            final = get_recompute_job(job_id) or {}
            status = str(final.get("status") or "")
            if status not in {"complete", "failed"}:
                if proc.exitcode not in (0, None):
                    _patch(
                        job_id,
                        status="failed",
                        percent=100,
                        message=f"Recompute process exited ({proc.exitcode})",
                        error="process_exit",
                        finished_at=_utcnow(),
                    )
                else:
                    # Child wrote complete/failed to disk; sync into memory.
                    with _LOCK:
                        if job_id in _JOBS:
                            _JOBS[job_id].update(
                                {
                                    k: final[k]
                                    for k in final
                                    if k in _JOBS[job_id] or k
                                    in {
                                        "before",
                                        "after",
                                        "status",
                                        "percent",
                                        "message",
                                        "error",
                                        "updated",
                                        "total",
                                        "skipped_no_excerpt",
                                        "finished_at",
                                    }
                                }
                            )
                            _JOBS[job_id]["updated_at"] = _utcnow()
            else:
                with _LOCK:
                    if job_id in _JOBS:
                        _JOBS[job_id].update(final)
                        _JOBS[job_id]["updated_at"] = _utcnow()
            logger.info(
                "recompute process done job=%s status=%s exit=%s",
                job_id[:8],
                (get_recompute_job(job_id) or {}).get("status"),
                proc.exitcode,
            )
        except Exception as exc:
            logger.exception("recompute coordinator failed job=%s", job_id[:8])
            _patch(
                job_id,
                status="failed",
                percent=100,
                message=f"{exc.__class__.__name__}: {exc}",
                error=str(exc)[:500],
                finished_at=_utcnow(),
            )
            if proc.is_alive():
                try:
                    proc.terminate()
                    proc.join(timeout=5)
                except Exception:
                    pass
        finally:
            release_local_slot(job_id)

    threading.Thread(
        target=_coordinator, daemon=True, name=f"recompute-coord-{job_id[:8]}"
    ).start()
    return dict(row)
