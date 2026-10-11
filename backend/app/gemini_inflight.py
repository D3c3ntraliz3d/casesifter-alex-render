"""Cap concurrent Gemini work by call count and total payload bytes in flight.

Gemini stage runs in the web process. Base64 PDF bodies must not accumulate
unbounded; callers acquire before encoding/sending and release immediately after.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_LOCK = threading.Lock()
_COND = threading.Condition(_LOCK)
_INFLIGHT_BYTES = 0
_INFLIGHT_CALLS = 0
_SEM: threading.Semaphore | None = None
_MAX_CALLS: int | None = None
_MAX_BYTES: int | None = None


def _ensure() -> None:
    global _SEM, _MAX_CALLS, _MAX_BYTES
    if _SEM is not None:
        return
    from app.config import settings

    _MAX_CALLS = max(1, int(settings.gemini_max_concurrent or 2))
    mb = max(16, int(settings.gemini_max_inflight_mb or 150))
    _MAX_BYTES = mb * 1024 * 1024
    _SEM = threading.Semaphore(_MAX_CALLS)
    logger.info(
        "gemini inflight cap calls=%s bytes_mb=%s",
        _MAX_CALLS,
        mb,
    )


def max_concurrent() -> int:
    _ensure()
    assert _MAX_CALLS is not None
    return _MAX_CALLS


def max_inflight_bytes() -> int:
    _ensure()
    assert _MAX_BYTES is not None
    return _MAX_BYTES


def stats() -> dict[str, Any]:
    _ensure()
    with _LOCK:
        return {
            "inflight_calls": _INFLIGHT_CALLS,
            "inflight_mb": round(_INFLIGHT_BYTES / (1024 * 1024), 2),
            "max_calls": _MAX_CALLS,
            "max_mb": round((_MAX_BYTES or 0) / (1024 * 1024), 1),
        }


def acquire(payload_bytes: int, *, timeout: float = 600.0) -> bool:
    """Block until a call slot and byte budget are available."""
    global _INFLIGHT_BYTES, _INFLIGHT_CALLS
    _ensure()
    assert _SEM is not None and _MAX_BYTES is not None
    need = max(0, int(payload_bytes))
    # Oversized single payload: still allow one call (caller already size-capped).
    budget = max(_MAX_BYTES, need)
    deadline = time.monotonic() + max(1.0, float(timeout))
    if not _SEM.acquire(timeout=max(0.1, deadline - time.monotonic())):
        return False
    with _COND:
        while _INFLIGHT_BYTES + need > budget:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                _SEM.release()
                return False
            _COND.wait(timeout=min(1.0, remaining))
        _INFLIGHT_BYTES += need
        _INFLIGHT_CALLS += 1
    return True


def release(payload_bytes: int) -> None:
    global _INFLIGHT_BYTES, _INFLIGHT_CALLS
    _ensure()
    assert _SEM is not None
    need = max(0, int(payload_bytes))
    with _COND:
        _INFLIGHT_BYTES = max(0, _INFLIGHT_BYTES - need)
        _INFLIGHT_CALLS = max(0, _INFLIGHT_CALLS - 1)
        _COND.notify_all()
    _SEM.release()
