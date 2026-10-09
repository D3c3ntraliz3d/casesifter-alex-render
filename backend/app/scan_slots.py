"""Bounded concurrent scans with safe force-release when a job stalls.

Cap comes from SCAN_MAX_CONCURRENT (default 1 on the 512 MB analysis instance).
"""

from __future__ import annotations

import threading

_SLOTS: threading.Semaphore | None = None
_MAX: int | None = None
_HOLDERS: dict[str, bool] = {}
_LOCK = threading.Lock()


def _ensure() -> None:
    global _SLOTS, _MAX
    if _SLOTS is not None:
        return
    from app.config import settings

    _MAX = max(1, int(settings.scan_max_concurrent or 1))
    _SLOTS = threading.Semaphore(_MAX)


def acquire(job_id: str, *, timeout: float = 600) -> bool:
    _ensure()
    assert _SLOTS is not None
    if not _SLOTS.acquire(timeout=timeout):
        return False
    with _LOCK:
        _HOLDERS[job_id] = True
    return True


def release(job_id: str) -> None:
    _ensure()
    assert _SLOTS is not None
    with _LOCK:
        if _HOLDERS.pop(job_id, False):
            _SLOTS.release()


def force_release(job_id: str) -> bool:
    """Release slot if this job still holds it (stalled worker may still be running)."""
    _ensure()
    assert _SLOTS is not None
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
    _ensure()
    assert _MAX is not None
    return _MAX
