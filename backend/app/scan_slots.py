"""Bounded concurrent scans with safe force-release when a job stalls."""

from __future__ import annotations

import threading

_MAX = 2
_SLOTS = threading.Semaphore(_MAX)
_HOLDERS: dict[str, bool] = {}
_LOCK = threading.Lock()


def acquire(job_id: str, *, timeout: float = 600) -> bool:
    if not _SLOTS.acquire(timeout=timeout):
        return False
    with _LOCK:
        _HOLDERS[job_id] = True
    return True


def release(job_id: str) -> None:
    with _LOCK:
        if _HOLDERS.pop(job_id, False):
            _SLOTS.release()


def force_release(job_id: str) -> bool:
    """Release slot if this job still holds it (stalled worker may still be running)."""
    with _LOCK:
        if _HOLDERS.pop(job_id, False):
            _SLOTS.release()
            return True
    return False


def active_job_ids() -> list[str]:
    with _LOCK:
        return list(_HOLDERS.keys())


def active_count() -> int:
    with _LOCK:
        return len(_HOLDERS)


def max_slots() -> int:
    return _MAX
