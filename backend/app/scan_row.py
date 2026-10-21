"""Document-list status while a rescan has not written its new report yet."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Callable

_ACTIVE = {"queued", "running", "local_done"}
_FAILED = {"failed"}
_CANCELLED = {"cancelled"}
_INTERRUPTED = {"interrupted"}
_TERMINAL_OPEN = _FAILED | _INTERRUPTED | _CANCELLED
# Complete jobs without a document row yet (persist lag). Keep visible briefly.
_UNATTACHED_COMPLETE_WINDOW = timedelta(hours=6)


def _job_updated_at(job: dict[str, Any]) -> datetime | None:
    raw = job.get("updated_at") or job.get("created_at")
    if not raw:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo else raw.replace(tzinfo=timezone.utc)
    text = str(raw).strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def _provisional_label(job: dict[str, Any], sha: str) -> tuple[str, str]:
    """Prefer durable job labels, then progress filename, then a short hash."""
    display = (
        str(job.get("display_name") or "").strip()
        or str(job.get("original_filename") or "").strip()
        or str(job.get("filename") or "").strip()
    )
    original = (
        str(job.get("original_filename") or "").strip()
        or str(job.get("filename") or "").strip()
        or display
    )
    if not display:
        short = (sha[:12] + "…") if len(sha) > 12 else (sha or "upload")
        display = f"Upload {short}"
        original = original or display
    return display, original or display


def _apply_job_status(row: dict[str, Any], job: dict[str, Any], status: str) -> None:
    job_id = str(job.get("job_id") or "")
    if status in _ACTIVE:
        if status == "local_done":
            row["status"] = "local_done"
        elif status == "queued":
            row["status"] = "queued"
        else:
            row["status"] = "scanning"
        row["stage"] = job.get("stage") or ("queued" if status == "queued" else status)
        row["scan_in_progress"] = True
        row["scan_failed_open"] = False
        row["risk_level"] = None
        row["risk_score"] = None
        row["ai_review"] = None
        row["ai_review_reason"] = None
        row["scan_job_id"] = job_id
        # Keep prior module rows only for which dots to show (yellow). Do not
        # publish live ok/error colors here — that flashed every check green
        # as soon as local_done wrote scan_modules.
    elif status in _INTERRUPTED:
        row["status"] = "interrupted"
        row["stage"] = job.get("stage")
        row["scan_in_progress"] = False
        row["scan_failed_open"] = True
        row["risk_level"] = None
        row["risk_score"] = None
        row["ai_review"] = None
        row["ai_review_reason"] = None
        row["last_error"] = job.get("error") or job.get("message")
        row["scan_job_id"] = job_id
    elif status in _FAILED:
        row["status"] = "failed"
        row["stage"] = job.get("stage")
        row["scan_in_progress"] = False
        row["scan_failed_open"] = True
        row["risk_level"] = None
        row["risk_score"] = None
        row["ai_review"] = None
        row["ai_review_reason"] = None
        row["last_error"] = job.get("error") or job.get("message")
        row["scan_job_id"] = job_id
    elif status in _CANCELLED:
        row["status"] = "cancelled"
        row["stage"] = job.get("stage") or "cancelled"
        row["scan_in_progress"] = False
        row["scan_failed_open"] = True
        row["risk_level"] = None
        row["risk_score"] = None
        row["ai_review"] = None
        row["ai_review_reason"] = None
        row["last_error"] = job.get("error") or job.get("message") or "cancelled_by_user"
        row["scan_job_id"] = job_id
    elif status == "complete":
        # Job finished but last_job_id still points at the old report, or
        # persist has not created a document row yet.
        row["status"] = "scanning"
        row["stage"] = job.get("stage") or "generating_report"
        row["scan_in_progress"] = True
        row["scan_failed_open"] = False
        row["risk_level"] = None
        row["risk_score"] = None
        row["ai_review"] = None
        row["ai_review_reason"] = None
        row["scan_job_id"] = job_id


def _should_list_unattached_job(job: dict[str, Any], status: str, *, now: datetime) -> bool:
    """Jobs without a persisted document must still appear on the matter list."""
    if status in _ACTIVE or status in _TERMINAL_OPEN:
        return True
    if status != "complete":
        return False
    # Brief persist lag after analysis finishes — not intentional deletes
    # (delete clears matter_id on the job).
    updated = _job_updated_at(job)
    if updated is None:
        return True
    return updated >= now - _UNATTACHED_COMPLETE_WINDOW


def _provisional_doc_from_job(sha: str, job: dict[str, Any], status: str) -> dict[str, Any]:
    job_id = str(job.get("job_id") or "")
    display, original = _provisional_label(job, sha)
    kind = str(job.get("doc_kind") or job.get("doc_type") or "").strip() or None
    row: dict[str, Any] = {
        "id": f"pending:{job_id or sha}",
        "sha256": sha,
        "original_filename": original,
        "display_name": display,
        "doc_kind": kind,
        "doc_type": kind,
        "has_report": False,
        "pending_upload": True,
        "provisional": True,
    }
    _apply_job_status(row, job, status)
    return row


def overlay_inflight_scans(
    docs: list[dict[str, Any]] | None,
    jobs_by_sha: dict[str, dict[str, Any]] | None,
) -> list[dict[str, Any]]:
    """Hide the previous Complete / risk while a newer job is still open.

    A filed date is not touched. Early local-module persist may already set
    last_job_id to the in-flight job — still treat active durable jobs as
    in progress so the row does not flash green Complete mid-scan.

    Durable ``interrupted`` stays ``interrupted`` on the list (not ``failed``).

    Hosted uploads create an analysis job before a document row exists. Jobs
    whose sha is not yet in ``docs`` are appended as provisional rows so the
    Documents table never silently drops accepted uploads.
    """
    out: list[dict[str, Any]] = []
    jobs = jobs_by_sha or {}
    seen_shas: set[str] = set()
    for doc in docs or []:
        row = dict(doc)
        sha = str(row.get("sha256") or "").lower()
        if sha:
            seen_shas.add(sha)
        job = jobs.get(sha)
        if not isinstance(job, dict):
            out.append(row)
            continue
        job_id = str(job.get("job_id") or "")
        status = str(job.get("status") or "")
        stored = str(row.get("scan_job_id") or "")
        if not job_id:
            out.append(row)
            continue
        # Fully attached finished job — leave the saved Complete / modules alone.
        if job_id == stored and status == "complete":
            out.append(row)
            continue
        # Newer complete exists while last_job_id still points at an old interrupt —
        # keep Scanning until persist attaches the complete report.
        if status == "complete" and stored and job_id != stored:
            _apply_job_status(row, job, "complete")
            out.append(row)
            continue
        # No open work for this hash.
        if status not in _ACTIVE and status not in _TERMINAL_OPEN and status != "complete":
            out.append(row)
            continue
        _apply_job_status(row, job, status)
        out.append(row)

    now = datetime.now(timezone.utc)
    for sha, job in jobs.items():
        if not sha or sha in seen_shas or not isinstance(job, dict):
            continue
        status = str(job.get("status") or "")
        if not _should_list_unattached_job(job, status, now=now):
            continue
        if not str(job.get("job_id") or ""):
            continue
        out.append(_provisional_doc_from_job(sha, job, status))
    return out


def attach_vault_scan_progress(
    docs: list[dict[str, Any]] | None,
    load_progress: Callable[[str], Any],
) -> list[dict[str, Any]]:
    """Attach the live job stage and drop the previous risk while a vault scan runs."""
    scanning = {"queued", "scanning"}
    out: list[dict[str, Any]] = []
    for doc in docs or []:
        row = dict(doc)
        status = str(row.get("status") or "")
        job_id = row.get("scan_job_id")
        if status in scanning and job_id:
            row["scan_in_progress"] = True
            row["risk_level"] = None
            row["risk_score"] = None
            row["ai_review"] = None
            try:
                progress = load_progress(str(job_id))
            except Exception:
                progress = None
            if progress is not None:
                row["stage"] = getattr(progress, "stage", None)
                pstatus = getattr(progress, "status", None)
                if pstatus == "running":
                    row["status"] = "scanning"
                elif pstatus == "local_done":
                    row["status"] = "local_done"
                elif pstatus == "queued":
                    row["status"] = "queued"
                elif pstatus in _INTERRUPTED:
                    row["status"] = "interrupted"
                    row["scan_in_progress"] = False
                    row["scan_failed_open"] = True
                    row["last_error"] = getattr(progress, "error", None) or getattr(progress, "message", None)
                elif pstatus in _FAILED:
                    row["status"] = "failed"
                    row["scan_in_progress"] = False
                    row["scan_failed_open"] = True
                    row["last_error"] = getattr(progress, "error", None) or getattr(progress, "message", None)
        out.append(row)
    return out
