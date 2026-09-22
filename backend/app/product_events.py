"""Privacy-safe product telemetry — no paths, captions, or document text."""

from __future__ import annotations

import time
from typing import Any

from app import cases_db
from app.telemetry import share_enabled

# Event types shipped to control plane (append-only jsonl).
APP_OPEN = "app_open"
APP_VERSION = "app_version"
SCAN_STARTED = "scan_started"
SCAN_COMPLETED = "scan_completed"
SCAN_FAILED = "scan_failed"
SCAN_PROGRESS_STALL = "scan_progress_stall"
CHAT_ASKED = "chat_asked"


def file_mb_bucket(size_bytes: int | None) -> str:
    if not size_bytes or size_bytes <= 0:
        return "unknown"
    mb = size_bytes / (1024 * 1024)
    if mb < 1:
        return "lt_1mb"
    if mb < 5:
        return "1_5mb"
    if mb < 20:
        return "5_20mb"
    if mb < 50:
        return "20_50mb"
    return "50mb_plus"


def page_count_bucket(pages: int | None) -> str:
    if pages is None or pages <= 0:
        return "unknown"
    if pages <= 5:
        return "1_5"
    if pages <= 20:
        return "6_20"
    if pages <= 50:
        return "21_50"
    return "51_plus"


def error_class_from_message(message: str | None) -> str:
    text = (message or "").lower()
    if "timeout" in text or "timed out" in text or "stall" in text:
        return "timeout"
    if "memory" in text or "oom" in text:
        return "oom"
    if "401" in text or "402" in text or "auth" in text or "subscription" in text:
        return "auth"
    if "api" in text or "502" in text or "503" in text or "unreachable" in text:
        return "api"
    return "other"


def record_product_event(event_type: str, payload: dict[str, Any] | None = None) -> str | None:
    """Queue only — never flush inline (flush blocked scans when control plane was slow)."""
    if not share_enabled():
        return None
    body = {"ts": time.time(), **(payload or {})}
    return cases_db.record_telemetry(event_type, body)


def emit_scan_started(
    *,
    job_id: str,
    doc_kind: str | None = None,
    page_count: int | None = None,
    size_bytes: int | None = None,
) -> str | None:
    return record_product_event(
        SCAN_STARTED,
        {
            "job_id": job_id[:32],
            "doc_kind": doc_kind or "unknown",
            "page_count_bucket": page_count_bucket(page_count),
            "file_mb_bucket": file_mb_bucket(size_bytes),
        },
    )


def emit_scan_completed(
    *,
    job_id: str,
    duration_ms: int | float | None,
    risk_level: str | None,
    finding_counts: dict[str, int] | None = None,
) -> str | None:
    return record_product_event(
        SCAN_COMPLETED,
        {
            "job_id": job_id[:32],
            "duration_ms": int(duration_ms or 0),
            "risk_level": risk_level or "unknown",
            "finding_counts": finding_counts or {},
        },
    )


def emit_scan_failed(
    *,
    job_id: str,
    stage: str | None,
    error_class: str | None = None,
    error_message: str | None = None,
) -> str | None:
    return record_product_event(
        SCAN_FAILED,
        {
            "job_id": job_id[:32],
            "stage": stage or "unknown",
            "error_class": error_class or error_class_from_message(error_message),
        },
    )


def emit_scan_progress_stall(
    *,
    job_id: str,
    stage: str | None,
    minutes_since_update: float,
) -> str | None:
    return record_product_event(
        SCAN_PROGRESS_STALL,
        {
            "job_id": job_id[:32],
            "stage": stage or "unknown",
            "minutes_since_update": round(float(minutes_since_update), 1),
        },
    )


def emit_chat_asked(*, used_model: bool, hit_count: int, question_len: int) -> str | None:
    return record_product_event(
        CHAT_ASKED,
        {
            "used_model": bool(used_model),
            "hit_count": int(hit_count),
            "question_len_bucket": _len_bucket(question_len),
        },
    )


def _len_bucket(n: int) -> str:
    if n <= 0:
        return "0"
    if n <= 40:
        return "1_40"
    if n <= 200:
        return "41_200"
    return "201_plus"


def finding_count_summary(findings: list[Any]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in findings:
        sev = getattr(f, "severity", None) or (f.get("severity") if isinstance(f, dict) else "unknown")
        counts[str(sev)] = counts.get(str(sev), 0) + 1
    return counts
