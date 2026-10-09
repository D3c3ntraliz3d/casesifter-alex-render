"""Picklable entry point for ProcessPoolExecutor child processes.

Must stay at module top level. Resets DB engines after fork/spawn.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def execute_ephemeral_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Run one ephemeral scan in a child process. payload is JSON-friendly."""
    # Child must not reuse the parent's SQLAlchemy connections.
    try:
        from app.fact_memory.models import reset_engine

        reset_engine()
    except Exception:
        logger.debug("reset_engine skipped", exc_info=True)

    from app.ephemeral import delete_ephemeral_source, maybe_delete_after_terminal
    from app.pipeline import run_pipeline
    from app.product_events import emit_scan_failed
    from app.resource import rss_mb
    from app.storage import load_progress, update_job
    from app.v1_analysis import (
        _persist_pipeline_result,
        _resolve_upload_labels,
        _update_durable_job,
    )

    job_id = str(payload["job_id"])
    dest = Path(payload["dest"])
    submitted_by = payload.get("submitted_by") or "web-app"
    display_name = payload.get("display_name")
    doc_kind = payload.get("doc_kind")
    prior_snapshots = payload.get("prior_snapshots") or []
    matter_id = payload.get("matter_id")
    firm_ctx = payload.get("firm_ctx")
    filed_date = payload.get("filed_date")
    persist = bool(payload.get("persist"))

    peak = {"mb": rss_mb() or 0.0}
    stop = threading.Event()

    def _sample() -> None:
        while not stop.wait(2.0):
            cur = rss_mb()
            if cur is not None and cur > peak["mb"]:
                peak["mb"] = cur

    sampler = threading.Thread(target=_sample, daemon=True, name=f"rss-{job_id[:8]}")
    sampler.start()

    logger.info(
        "ephemeral child start job=%s rss_mb=%s",
        job_id[:8],
        rss_mb(),
    )
    terminal_status = "failed"
    terminal_message = None
    terminal_error = None
    document_id = None
    try:
        _update_durable_job(job_id, status="running", message="Scan running in worker process.")
        try:
            sample = ""
            if not (doc_kind or "").strip():
                suffix = dest.suffix.lower()
                if suffix == ".pdf":
                    try:
                        from app.scan_profile import peek_pdf_text

                        sample = peek_pdf_text(dest)[:4000]
                    except Exception:
                        sample = ""
                elif suffix in {".txt", ".text"}:
                    try:
                        sample = dest.read_text(encoding="utf-8", errors="ignore")[:4000]
                    except OSError:
                        sample = ""
            resolved_name, resolved_kind = _resolve_upload_labels(
                dest.name, display_name, doc_kind, sample
            )
            report = run_pipeline(
                job_id,
                dest,
                submitted_by=submitted_by,
                display_name=resolved_name,
                doc_kind=resolved_kind,
                prior_snapshots=prior_snapshots or None,
            )
            if persist and matter_id and firm_ctx and report is not None:
                document_id = _persist_pipeline_result(
                    job_id=job_id,
                    source=dest,
                    report=report,
                    matter_id=matter_id,
                    firm_ctx=firm_ctx,
                    display_name=resolved_name,
                    doc_kind=resolved_kind,
                    filed_date=filed_date,
                )
            progress = load_progress(job_id)
            if progress and progress.status == "complete":
                terminal_status = "complete"
                terminal_message = progress.message or "Analysis complete."
                terminal_error = None
            elif progress and progress.status == "failed":
                terminal_status = "failed"
                terminal_message = progress.message or "Analysis failed."
                terminal_error = progress.error
            else:
                terminal_status = "complete"
                terminal_message = "Analysis complete."
        except Exception as exc:
            emit_scan_failed(job_id=job_id, stage="failed", error_message=str(exc))
            update_job(
                job_id,
                status="failed",
                stage="failed",
                percent=100,
                message="Analysis failed.",
                error=str(exc),
            )
            terminal_status = "failed"
            terminal_message = "Analysis failed."
            terminal_error = str(exc)
        finally:
            maybe_delete_after_terminal(job_id, retain_report=persist)
    finally:
        stop.set()
        sampler.join(timeout=1.0)
        end_rss = rss_mb()
        if end_rss is not None and end_rss > peak["mb"]:
            peak["mb"] = end_rss
        _update_durable_job(
            job_id,
            status=terminal_status,
            message=terminal_message,
            error=terminal_error,
            document_id=document_id,
        )
        logger.info(
            "ephemeral child end job=%s status=%s rss_mb=%s peak_rss_mb=%s",
            job_id[:8],
            terminal_status,
            end_rss,
            round(peak["mb"], 1),
        )
        delete_ephemeral_source(job_id)
    return {
        "job_id": job_id,
        "status": terminal_status,
        "peak_rss_mb": peak["mb"],
    }
