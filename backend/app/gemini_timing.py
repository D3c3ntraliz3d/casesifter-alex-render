"""Per-thread Gemini wall-time accumulator for scan timing buckets."""

from __future__ import annotations

import threading
import time
from typing import Any

_local = threading.local()


def reset() -> None:
    _local.gemini_s = 0.0
    _local.calls = 0


def add(seconds: float) -> None:
    if not hasattr(_local, "gemini_s"):
        _local.gemini_s = 0.0
        _local.calls = 0
    _local.gemini_s += max(0.0, float(seconds))
    _local.calls = int(getattr(_local, "calls", 0) or 0) + 1


def snapshot() -> dict[str, Any]:
    return {
        "gemini_s": round(float(getattr(_local, "gemini_s", 0.0) or 0.0), 2),
        "calls": int(getattr(_local, "calls", 0) or 0),
    }


class GeminiTimer:
    """Context manager that attributes wall time to gemini_s."""

    def __enter__(self) -> "GeminiTimer":
        self._t0 = time.monotonic()
        return self

    def __exit__(self, *exc: object) -> None:
        add(time.monotonic() - self._t0)
