"""Per-scan Gemini wall-time + call summary (thread-safe across chunk workers)."""

from __future__ import annotations

import logging
import threading
import time
from typing import Any

logger = logging.getLogger(__name__)

_lock = threading.Lock()
_state: dict[str, Any] = {
    "gemini_s": 0.0,
    "calls": 0,
    "http_503": 0,
    "fallback_count": 0,
    "by_task_model_tier": {},  # "task|model|served_tier" -> int
}


def reset() -> None:
    with _lock:
        _state["gemini_s"] = 0.0
        _state["calls"] = 0
        _state["http_503"] = 0
        _state["fallback_count"] = 0
        _state["by_task_model_tier"] = {}


def add(seconds: float) -> None:
    with _lock:
        _state["gemini_s"] = float(_state.get("gemini_s") or 0.0) + max(0.0, float(seconds))


def note_http_503() -> None:
    with _lock:
        _state["http_503"] = int(_state.get("http_503") or 0) + 1


def note_fallback() -> None:
    with _lock:
        _state["fallback_count"] = int(_state.get("fallback_count") or 0) + 1


def note_success(
    *,
    task: str,
    model: str,
    requested_tier: str,
    served_tier: str,
) -> None:
    """Record one successful generateContent completion for the scan summary."""
    key = f"{task}|{model}|{served_tier or 'unknown'}"
    with _lock:
        _state["calls"] = int(_state.get("calls") or 0) + 1
        bucket = _state.setdefault("by_task_model_tier", {})
        bucket[key] = int(bucket.get(key) or 0) + 1


def snapshot() -> dict[str, Any]:
    with _lock:
        by = dict(_state.get("by_task_model_tier") or {})
        calls_detail: list[dict[str, Any]] = []
        for key, count in sorted(by.items()):
            parts = key.split("|", 2)
            calls_detail.append(
                {
                    "task": parts[0] if len(parts) > 0 else "?",
                    "model": parts[1] if len(parts) > 1 else "?",
                    "served_tier": parts[2] if len(parts) > 2 else "unknown",
                    "calls": int(count),
                }
            )
        return {
            "gemini_s": round(float(_state.get("gemini_s") or 0.0), 2),
            "calls": int(_state.get("calls") or 0),
            "http_503": int(_state.get("http_503") or 0),
            "fallback_count": int(_state.get("fallback_count") or 0),
            "by_task_model_tier": calls_detail,
        }


def log_scan_summary(*, job_id: str = "") -> dict[str, Any]:
    """Log + return the per-scan Gemini summary."""
    snap = snapshot()
    prefix = (job_id or "")[:8] or "-"
    bits = [
        f"{row['task']}/{row['model']}/served={row['served_tier']}:{row['calls']}"
        for row in snap.get("by_task_model_tier") or []
    ]
    logger.info(
        "gemini scan summary job=%s calls=%s http_503=%s fallbacks=%s "
        "wait_s=%s by_task_model_served_tier=[%s]",
        prefix,
        snap.get("calls"),
        snap.get("http_503"),
        snap.get("fallback_count"),
        snap.get("gemini_s"),
        "; ".join(bits) if bits else "-",
    )
    return snap


class GeminiTimer:
    """Context manager that attributes wall time (including retry backoff) to gemini_s."""

    def __enter__(self) -> "GeminiTimer":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        add(time.monotonic() - self._t0)
