"""Internal API: worker posts progress + results (no Postgres access from worker)."""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Any

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

from app.config import settings
from app.models import Report
from app.storage import TERMINAL_STATUSES, create_job, load_progress, update_job
from app.worker_auth import verify_worker_request

logger = logging.getLogger(__name__)

internal_router = APIRouter(prefix="/internal/v1", tags=["internal-worker"])


class ProgressBody(BaseModel):
    status: str | None = None
    stage: str | None = None
    percent: int | None = None
    message: str | None = None
    error: str | None = None


class ResultBody(BaseModel):
    status: str = Field(..., description="complete or failed")
    message: str | None = None
    error: str | None = None
    report: dict[str, Any] | None = None
    ai_review: str | None = None
    ai_review_reason: str | None = None
    attempt: int | None = None
    source_name: str | None = None
    display_name: str | None = None
    doc_kind: str | None = None
    filed_date: str | None = None
    # Ignored for ownership — firm/matter/user come from the durable job record.
    # Present only so a mismatched body can be rejected.
    matter_id: str | None = None
    firm_id: str | None = None
    user_id: str | None = None
    persist: bool = False
    # Optional source size for persist when bytes are already gone.
    source_size: int | None = None
    sha256: str | None = None
    # Durable matter source key written by the worker (SOURCE_FILE_RETENTION=s3).
    source_blob_key: str | None = None


def _check_auth(request: Request, job_id: str, body: bytes) -> None:
    verify_worker_request(
        job_id=job_id,
        authorization=request.headers.get("authorization"),
        x_cs_timestamp=request.headers.get("x-cs-timestamp"),
        x_cs_signature=request.headers.get("x-cs-signature"),
        body=body,
    )


def _is_local_dev_env() -> bool:
    return (settings.app_env or "").strip().lower() in {"local", "development", "dev"}


def _resolve_job_ownership(job_id: str, body: ResultBody) -> dict[str, Any]:
    """Firm/matter/user come from the durable analysis_jobs row, never the body."""
    from app.fact_memory.store import get_store

    row = get_store().get_analysis_job(job_id)
    if row is None:
        if body.firm_id or body.matter_id or body.user_id or body.persist:
            raise HTTPException(404, "Unknown job — no durable job record.")
        return {"firm_id": None, "matter_id": None, "user_id": None, "persist": False}

    if body.firm_id and body.firm_id != row["firm_id"]:
        raise HTTPException(403, "firm_id does not match job record.")
    if body.matter_id and row.get("matter_id") and body.matter_id != row["matter_id"]:
        raise HTTPException(403, "matter_id does not match job record.")
    if body.user_id and row.get("user_id") and body.user_id != row["user_id"]:
        raise HTTPException(403, "user_id does not match job record.")

    return {
        "firm_id": row["firm_id"],
        "matter_id": row.get("matter_id"),
        "user_id": row.get("user_id"),
        "persist": bool(body.persist),
    }


@internal_router.post("/jobs/{job_id}/progress")
async def worker_progress(job_id: str, request: Request):
    raw = await request.body()
    _check_auth(request, job_id, raw)
    try:
        body = ProgressBody.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(400, "Invalid progress payload.") from exc

    progress = load_progress(job_id)
    if progress is None:
        create_job(job_id, body.message or "worker")
    try:
        update_job(
            job_id,
            status=body.status,  # type: ignore[arg-type]
            stage=body.stage,
            percent=body.percent,
            message=body.message,
            error=body.error,
            allow_rerun=False,
        )
    except FileNotFoundError:
        create_job(job_id, "worker")
        update_job(
            job_id,
            status=body.status,  # type: ignore[arg-type]
            stage=body.stage,
            percent=body.percent,
            message=body.message,
            error=body.error,
        )

    from app.v1_analysis import _update_durable_job

    _update_durable_job(
        job_id,
        status=body.status,
        message=body.message,
        error=body.error,
    )
    return {"ok": True, "job_id": job_id}


@internal_router.post("/jobs/{job_id}/result")
async def worker_result(job_id: str, request: Request):
    raw = await request.body()
    _check_auth(request, job_id, raw)
    try:
        body = ResultBody.model_validate_json(raw)
    except Exception as exc:
        raise HTTPException(400, "Invalid result payload.") from exc

    if body.status not in {"complete", "failed", "cancelled"}:
        raise HTTPException(400, "status must be complete, failed, or cancelled.")

    ownership = _resolve_job_ownership(job_id, body)

    # Idempotent: already terminal → accept without duplicating persist.
    existing = load_progress(job_id)
    if existing and existing.status in TERMINAL_STATUSES:
        logger.info(
            "worker result idempotent job=%s status=%s",
            job_id[:8],
            existing.status,
        )
        return {
            "ok": True,
            "job_id": job_id,
            "idempotent": True,
            "status": existing.status,
            "document_id": None,
        }

    if load_progress(job_id) is None:
        create_job(job_id, body.source_name or "document.pdf")

    document_id = None
    report_obj = None
    if body.status == "complete" and body.report:
        try:
            report_obj = Report.model_validate(body.report)
        except Exception as exc:
            raise HTTPException(400, f"Invalid report: {exc}") from exc
        update_job(
            job_id,
            status="complete",
            stage="complete",
            percent=100,
            message=body.message or "Analysis complete.",
            report=report_obj,
        )
        if (
            ownership["persist"]
            and ownership.get("matter_id")
            and ownership.get("firm_id")
            and report_obj is not None
        ):
            from app.v1_analysis import _persist_pipeline_result

            # Source bytes live only on the worker; persist uses report audit + stubs.
            stub = Path(body.source_name or "document.pdf")
            firm_ctx = {
                "firm_id": ownership["firm_id"],
                "user_id": ownership.get("user_id"),
            }
            document_id = _persist_pipeline_result(
                job_id=job_id,
                source=stub,
                report=report_obj,
                matter_id=ownership["matter_id"],
                firm_ctx=firm_ctx,
                display_name=body.display_name,
                doc_kind=body.doc_kind,
                filed_date=body.filed_date,
                source_blob_key=body.source_blob_key,
            )
    elif body.status == "cancelled":
        update_job(
            job_id,
            status="cancelled",
            stage="cancelled",
            percent=100,
            message=body.message or "Scan cancelled.",
            error=body.error or "cancelled_by_user",
        )
    else:
        update_job(
            job_id,
            status="failed",
            stage="failed",
            percent=100,
            message=body.message or "Analysis failed.",
            error=body.error or "worker_failed",
        )

    from app.v1_analysis import _update_durable_job

    _update_durable_job(
        job_id,
        status=body.status,
        message=body.message,
        error=body.error,
        document_id=document_id,
        ai_review=body.ai_review if body.status == "complete" else None,
        ai_review_reason=body.ai_review_reason if body.status == "complete" else None,
    )

    # Drop local job source if any leftover from upload staging.
    try:
        from app.ephemeral import delete_ephemeral_source, maybe_delete_after_terminal

        maybe_delete_after_terminal(job_id, retain_report=bool(ownership["persist"]))
        delete_ephemeral_source(job_id)
    except Exception:
        logger.debug("ephemeral cleanup after worker result failed job=%s", job_id[:8], exc_info=True)

    # Delete blob bundle (success and failure).
    try:
        from app.blob_store import get_blob_store, job_bundle_prefix

        get_blob_store().delete_prefix(job_bundle_prefix(job_id))
    except Exception:
        logger.warning("blob bundle delete failed job=%s", job_id[:8], exc_info=True)

    return {
        "ok": True,
        "job_id": job_id,
        "status": body.status,
        "document_id": document_id,
        "idempotent": False,
    }


@internal_router.put("/local-blobs/{key:path}")
async def local_blob_put(
    key: str,
    request: Request,
    expires: int | None = None,
    content_type: str | None = None,
):
    """Local signed-upload stand-in (Phase 2). Mounted only for local/dev."""
    if not _is_local_dev_env():
        raise HTTPException(404, "Not found.")

    auth = request.headers.get("authorization")
    from app.worker_auth import worker_service_token
    import hmac

    token = worker_service_token()
    if token and auth and auth.lower().startswith("bearer "):
        if not hmac.compare_digest(auth[7:].strip(), token):
            raise HTTPException(401, "Invalid token.")
    elif token:
        raise HTTPException(401, "Unauthorized.")

    from app.blob_store import get_blob_store

    data = await request.body()
    get_blob_store().put(key, data, content_type=content_type or "application/octet-stream")
    return {"ok": True, "key": key}
