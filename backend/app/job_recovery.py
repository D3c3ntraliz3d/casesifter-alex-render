"""Boot recovery for durable analysis_jobs — never leave local_done stuck."""

from __future__ import annotations

import json
import logging
from pathlib import Path

logger = logging.getLogger("uvicorn.error")


def recover_local_done_jobs() -> dict[str, int]:
    """Resume Gemini when artifacts+source exist; otherwise requeue from local."""
    from app.ephemeral import delete_ephemeral_source
    from app.fact_memory.store import get_store
    from app.gemini_executor import (
        gemini_payload_path,
        local_stage_path,
        source_present,
        submit_gemini_stage,
    )
    from app.scan_executor import submit_ephemeral_job
    from app.storage import job_dir, update_job

    store = get_store()
    rows = store.list_analysis_jobs_by_status(["local_done"])
    resumed = 0
    requeued = 0
    failed = 0
    for row in rows:
        job_id = row["job_id"]
        stage_ok = local_stage_path(job_id).is_file()
        payload_path = gemini_payload_path(job_id)
        payload: dict | None = None
        if payload_path.is_file():
            try:
                payload = json.loads(payload_path.read_text(encoding="utf-8"))
            except Exception:
                payload = None
        dest = (payload or {}).get("dest")
        src_ok = source_present(job_id, dest)

        if stage_ok and src_ok and payload and dest:
            try:
                logger.info("boot resume gemini job=%s", job_id[:8])
                submit_gemini_stage(payload)
                resumed += 1
            except Exception:
                logger.exception("boot gemini resume failed job=%s", job_id[:8])
                failed += 1
            continue

        # Missing artifacts or source — requeue from local if we still have a source
        # path recorded; otherwise mark interrupted so the client can re-submit.
        if payload and dest and Path(dest).is_file():
            try:
                # Restart recovery must not burn an attempt.
                store.update_analysis_job(
                    job_id,
                    status="queued",
                    message="Requeued from local after missing stage artifacts.",
                    error=None,
                )
                update_job(
                    job_id,
                    status="queued",
                    stage="queued",
                    percent=0,
                    message="Requeued from local after restart.",
                )
                # Drop stale stage files so local regenerates them.
                for name in ("local_stage.json",):
                    p = job_dir(job_id) / name
                    if p.exists():
                        try:
                            p.unlink()
                        except OSError:
                            pass
                submit_ephemeral_job(payload)
                requeued += 1
                logger.info("boot requeue local job=%s (no attempt bump)", job_id[:8])
            except Exception:
                logger.exception("boot local requeue failed job=%s", job_id[:8])
                failed += 1
            continue

        try:
            # Count as a running-at-restart hit (local_done was in-flight work).
            prior = int((row.get("restart_interruptions") or 0))
            matter_id = row.get("matter_id") or ""
            sha = (row.get("sha256") or "").lower()
            hist = 0
            if matter_id and sha:
                hist = store.max_restart_interruptions_for_sha(
                    firm_id=row["firm_id"], matter_id=matter_id, sha256=sha
                )
            new_count = max(prior, hist) + 1
            store.set_restart_interruptions(job_id, new_count)
            if new_count >= 2:
                msg = (
                    "Needs review — may be crashing the server "
                    f"(running at {new_count} instance restarts)."
                )
                store.update_analysis_job(
                    job_id,
                    status="failed",
                    message=msg,
                    error="restart_quarantine",
                )
                logger.error(
                    "quarantine sha=%s after %s restart interruptions (local_done) job=%s",
                    sha[:12],
                    new_count,
                    job_id[:8],
                )
                try:
                    update_job(
                        job_id,
                        status="failed",
                        stage="failed",
                        percent=100,
                        message=msg,
                        error="restart_quarantine",
                    )
                except Exception:
                    pass
            else:
                store.update_analysis_job(
                    job_id,
                    status="interrupted",
                    message="local_done artifacts/source missing after restart; re-submit required.",
                    error="local_done_missing_artifacts",
                )
                logger.warning(
                    "restart while local_done job=%s matter=%s sha=%s restart_interruptions=%s",
                    job_id[:8],
                    matter_id[:8],
                    sha[:12],
                    new_count,
                )
                try:
                    update_job(
                        job_id,
                        status="interrupted",
                        stage="interrupted",
                        percent=0,
                        message="Restarted with missing artifacts; re-submit required.",
                        error="local_done_missing_artifacts",
                    )
                except Exception:
                    pass
            delete_ephemeral_source(job_id)
            failed += 1
        except Exception:
            logger.exception("boot interrupt failed job=%s", job_id[:8])
            failed += 1

    return {"resumed": resumed, "requeued": requeued, "failed": failed, "seen": len(rows)}
