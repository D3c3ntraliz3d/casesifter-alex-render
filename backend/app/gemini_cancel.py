"""Per-job cancel flags for Gemini stage (threads cannot be killed)."""

from __future__ import annotations

import threading

_LOCK = threading.Lock()
_CANCELLED: set[str] = set()


def request_cancel(job_id: str) -> None:
    with _LOCK:
        _CANCELLED.add(job_id)


def clear_cancel(job_id: str) -> None:
    with _LOCK:
        _CANCELLED.discard(job_id)


def is_cancelled(job_id: str | None = None) -> bool:
    """If job_id is None, check thread-local current job."""
    jid = job_id
    if jid is None:
        jid = getattr(_tls, "job_id", None)
    if not jid:
        return False
    with _LOCK:
        return jid in _CANCELLED


_tls = threading.local()


def set_current_job(job_id: str | None) -> None:
    _tls.job_id = job_id


def current_job_id() -> str | None:
    return getattr(_tls, "job_id", None)


class CancelledError(RuntimeError):
    """Raised when a Gemini stage cancel flag is set between calls."""
