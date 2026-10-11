"""Picklable entry points for ProcessPoolExecutor (local) and Gemini thread pool.

Must stay at module top level. Resets DB engines after fork/spawn.
"""

from __future__ import annotations

import json
import logging
import threading
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)


def execute_ephemeral_job(payload: dict[str, Any]) -> dict[str, Any]:
    """Run local PDF/OCR stage in a child process. Parent then schedules Gemini."""
    try:
        from app.fact_memory.models import reset_engine

        reset_engine()
    except Exception:
        logger.debug("reset_engine skipped", exc_info=True)

    from app.ephemeral import delete_ephemeral_source
    from app.pipeline import run_local_stage
    from app.product_events import emit_scan_failed
    from app.resource import rss_mb
    from app.storage import update_job
    from app.v1_analysis import _resolve_upload_labels, _update_durable_job

    job_id = str(payload["job_id"])
    dest = Path(payload["dest"])
    submitted_by = payload.get("submitted_by") or "web-app"
    display_name = payload.get("display_name")
    doc_kind = payload.get("doc_kind")
    prior_snapshots = payload.get("prior_snapshots") or []

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
        "ephemeral local start job=%s rss_mb=%s",
        job_id[:8],
        rss_mb(),
    )
    terminal_status = "failed"
    terminal_message = None
    terminal_error = None
    local_s = None
    try:
        _update_durable_job(job_id, status="running", message="Local PDF/OCR running in worker process.")
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
            result = run_local_stage(
                job_id,
                dest,
                submitted_by=submitted_by,
                display_name=resolved_name,
                doc_kind=resolved_kind,
                prior_snapshots=prior_snapshots or None,
            )
            local_s = (result or {}).get("local_s")
            terminal_status = "local_done"
            terminal_message = "Local PDF/OCR complete — queued for Gemini review."
            terminal_error = None
            # Persist resume payload for Gemini stage / boot recovery.
            from app.gemini_executor import gemini_payload_path

            resume = {
                **payload,
                "display_name": resolved_name,
                "doc_kind": resolved_kind,
                "peak_rss_mb": peak["mb"],
                "local_s": local_s,
            }
            gemini_payload_path(job_id).write_text(
                json.dumps(resume, default=str), encoding="utf-8"
            )
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
            delete_ephemeral_source(job_id)
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
        )
        logger.info(
            "ephemeral local end job=%s status=%s rss_mb=%s peak_rss_mb=%s local_s=%s",
            job_id[:8],
            terminal_status,
            end_rss,
            round(peak["mb"], 1),
            local_s,
        )
    return {
        "job_id": job_id,
        "status": terminal_status,
        "peak_rss_mb": peak["mb"],
        "local_s": local_s,
        # Never echo `payload` back: it carries every prior document's facts and
        # text excerpts (megabytes on a large matter). Sending that through the
        # multiprocessing queue kept the child from exiting and deadlocked the
        # local slot. The parent already holds the payload; Gemini resume data
        # is on disk (gemini_payload.json).
    }


def execute_gemini_stage(payload: dict[str, Any]) -> dict[str, Any]:
    """Run Gemini stage in the web process. Deletes source on every terminal path."""
    from app.ephemeral import delete_ephemeral_source, maybe_delete_after_terminal
    from app.gemini_cancel import CancelledError, clear_cancel, set_current_job
    from app.pipeline import run_gemini_stage
    from app.product_events import emit_scan_failed
    from app.resource import rss_mb
    from app.storage import TERMINAL_STATUSES, load_progress, update_job
    from app.v1_analysis import _persist_pipeline_result, _update_durable_job

    job_id = str(payload["job_id"])
    dest = Path(payload["dest"])
    persist = bool(payload.get("persist"))
    matter_id = payload.get("matter_id")
    firm_ctx = payload.get("firm_ctx")
    filed_date = payload.get("filed_date")
    display_name = payload.get("display_name")
    doc_kind = payload.get("doc_kind")
    peak_rss = payload.get("peak_rss_mb")

    set_current_job(job_id)
    web_rss = rss_mb()
    logger.info(
        "gemini stage start job=%s web_rss_mb=%s peak_rss_mb=%s",
        job_id[:8],
        web_rss,
        peak_rss,
    )
    # If already sticky-failed by stage waiter, exit without overwriting.
    existing = load_progress(job_id)
    if existing and existing.status in TERMINAL_STATUSES:
        logger.info(
            "gemini stage skip — already terminal job=%s status=%s",
            job_id[:8],
            existing.status,
        )
        set_current_job(None)
        return {"job_id": job_id, "status": existing.status, "timing": None, "web_rss_mb": web_rss}

    _update_durable_job(job_id, status="running", message="Gemini review running.")
    update_job(
        job_id,
        status="running",
        stage="contextual",
        percent=88,
        message="Gemini review running…",
    )

    terminal_status = "failed"
    terminal_message = "Analysis failed."
    terminal_error = None
    document_id = None
    timing = None
    ai_review = None
    ai_review_reason = None
    try:
        try:
            # Source may be gone on AI-only re-run; prefer path if present.
            source_path = Path(dest) if dest else Path(".")
            if not source_path.is_file():
                from app.storage import job_dir as _job_dir

                stage_path = _job_dir(job_id) / "local_stage.json"
                source_name = "document.pdf"
                if stage_path.is_file():
                    try:
                        source_name = (
                            json.loads(stage_path.read_text(encoding="utf-8")).get("source_name")
                            or source_name
                        )
                    except Exception:
                        pass
                source_path = _job_dir(job_id) / Path(str(source_name)).name
            report = run_gemini_stage(job_id, source_path, peak_rss_mb=peak_rss)
            if persist and matter_id and firm_ctx and report is not None:
                document_id = _persist_pipeline_result(
                    job_id=job_id,
                    source=source_path,
                    report=report,
                    matter_id=matter_id,
                    firm_ctx=firm_ctx,
                    display_name=display_name,
                    doc_kind=doc_kind,
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
            if report is not None:
                ai_review = getattr(report, "ai_review", None)
                ai_review_reason = getattr(report, "ai_review_reason", None)
            else:
                ai_review = None
                ai_review_reason = None
            try:
                from app.storage import job_dir

                tpath = job_dir(job_id) / "timing.json"
                if tpath.is_file():
                    timing = json.loads(tpath.read_text(encoding="utf-8"))
            except Exception:
                timing = None
        except CancelledError as exc:
            # Stage waiter already sticky-failed; do not fight it.
            progress = load_progress(job_id)
            terminal_status = (progress.status if progress else None) or "failed"
            terminal_message = (progress.message if progress else None) or "Gemini stage cancelled."
            terminal_error = (progress.error if progress else None) or str(exc)
            logger.info("gemini stage cancelled job=%s", job_id[:8])
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
            delete_ephemeral_source(job_id)
    finally:
        clear_cancel(job_id)
        set_current_job(None)
        _update_durable_job(
            job_id,
            status=terminal_status,
            message=terminal_message,
            error=terminal_error,
            document_id=document_id,
            ai_review=ai_review if terminal_status == "complete" else None,
            ai_review_reason=ai_review_reason if terminal_status == "complete" else None,
        )
        logger.info(
            "gemini stage end job=%s status=%s web_rss_mb=%s timing=%s",
            job_id[:8],
            terminal_status,
            rss_mb(),
            timing,
        )
    return {
        "job_id": job_id,
        "status": terminal_status,
        "timing": timing,
        "web_rss_mb": rss_mb(),
    }
