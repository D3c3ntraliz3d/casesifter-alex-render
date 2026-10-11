"""Process one scan job bundle end-to-end (no Postgres)."""

from __future__ import annotations

import json
import logging
import shutil
import tempfile
from pathlib import Path
from typing import Any

from app.blob_store import get_blob_store, job_meta_key, job_source_key
from app.config import settings
from app.job_queue import QueueMessage
from app.pipeline import run_gemini_stage, run_local_stage
from app.storage import create_job, job_dir
from worker.client import post_progress, post_result

logger = logging.getLogger(__name__)


class WorkerJobError(RuntimeError):
    pass


def process_queue_message(message: QueueMessage) -> None:
    """Download bundle, run pipeline, POST result, delete bundle on every terminal path."""
    job_id = message.job_id
    store = get_blob_store()
    work = Path(tempfile.mkdtemp(prefix=f"cs-worker-{job_id[:8]}-"))
    # Pipeline artifacts stay in this temp workspace. Progress/report are owned by
    # the analysis API (callbacks), so restore settings.data_dir before POSTing.
    original_data_dir = settings.data_dir
    (work / "jobs").mkdir(parents=True, exist_ok=True)

    meta: dict[str, Any] = {}
    source_path: Path | None = None
    try:
        meta_raw = store.get(job_meta_key(job_id))
        meta = json.loads(meta_raw.decode("utf-8"))
        source_name = str(meta.get("source_name") or "document.pdf")
        source_path = work / "jobs" / job_id / Path(source_name).name
        source_path.parent.mkdir(parents=True, exist_ok=True)
        store.get_file(job_source_key(job_id, source_name), source_path)

        settings.data_dir = work
        create_job(job_id, source_name)
        # Progress callbacks use the analysis service's data_dir — restore first.
        settings.data_dir = original_data_dir
        post_progress(
            job_id,
            status="running",
            stage="local",
            percent=5,
            message="Worker started local PDF/OCR…",
        )

        settings.data_dir = work
        run_local_stage(
            job_id,
            source_path,
            submitted_by=str(meta.get("submitted_by") or "worker"),
            case_id=meta.get("matter_id"),
            document_id=None,
            display_name=meta.get("display_name"),
            doc_kind=meta.get("doc_kind"),
            prior_snapshots=meta.get("prior_snapshots") or [],
        )
        settings.data_dir = original_data_dir
        post_progress(
            job_id,
            status="local_done",
            stage="local_done",
            percent=82,
            message="Local PDF/OCR complete — Gemini review…",
        )

        settings.data_dir = work
        report = run_gemini_stage(job_id, source_path)
        from app.storage import load_progress

        progress = load_progress(job_id)
        status = (progress.status if progress else None) or "complete"
        status_message = (progress.message if progress else None) or "Analysis complete."
        error = progress.error if progress else None
        report_dict = report.model_dump(mode="json") if report is not None else None
        ai_review = getattr(report, "ai_review", None) if report is not None else None
        ai_review_reason = getattr(report, "ai_review_reason", None) if report is not None else None
        settings.data_dir = original_data_dir

        # Prefer sticky failed from pipeline if present.
        if status == "failed":
            post_result(
                job_id,
                {
                    "status": "failed",
                    "message": status_message,
                    "error": error or "pipeline_failed",
                    "attempt": message_attempt(message_obj=message, meta=meta),
                    "source_name": source_name,
                    "display_name": meta.get("display_name"),
                    "doc_kind": meta.get("doc_kind"),
                    "filed_date": meta.get("filed_date"),
                    "matter_id": meta.get("matter_id"),
                    "firm_id": meta.get("firm_id"),
                    "user_id": meta.get("user_id"),
                    "persist": bool(meta.get("persist")),
                },
            )
            return

        post_result(
            job_id,
            {
                "status": "complete",
                "message": status_message,
                "report": report_dict,
                "ai_review": ai_review,
                "ai_review_reason": ai_review_reason,
                "attempt": message_attempt(message_obj=message, meta=meta),
                "source_name": source_name,
                "display_name": meta.get("display_name"),
                "doc_kind": meta.get("doc_kind"),
                "filed_date": meta.get("filed_date"),
                "matter_id": meta.get("matter_id"),
                "firm_id": meta.get("firm_id"),
                "user_id": meta.get("user_id"),
                "persist": bool(meta.get("persist")),
                "source_size": int(source_path.stat().st_size) if source_path and source_path.is_file() else None,
                "sha256": (report.audit.sha256 if report and report.audit else None),
            },
        )
    except Exception as exc:
        logger.exception("worker job failed job=%s", job_id[:8])
        settings.data_dir = original_data_dir
        try:
            post_result(
                job_id,
                {
                    "status": "failed",
                    "message": "Analysis failed.",
                    "error": str(exc)[:500],
                    "attempt": message_attempt(message_obj=message, meta=meta),
                    "source_name": meta.get("source_name"),
                    "display_name": meta.get("display_name"),
                    "doc_kind": meta.get("doc_kind"),
                    "filed_date": meta.get("filed_date"),
                    "matter_id": meta.get("matter_id"),
                    "firm_id": meta.get("firm_id"),
                    "user_id": meta.get("user_id"),
                    "persist": bool(meta.get("persist")),
                },
            )
        except Exception:
            logger.exception("failed to POST failure result job=%s", job_id[:8])
        raise WorkerJobError(str(exc)) from exc
    finally:
        settings.data_dir = original_data_dir
        shutil.rmtree(work, ignore_errors=True)
        # Bundle delete is also done by the API on accepted result; belt-and-suspenders here.
        try:
            store.delete_prefix(f"jobs/{job_id}")
        except Exception:
            logger.debug("worker bundle delete failed job=%s", job_id[:8], exc_info=True)


def message_attempt(*, message_obj: QueueMessage, meta: dict[str, Any]) -> int:
    return int(message_obj.attempt or meta.get("attempt") or 1)
