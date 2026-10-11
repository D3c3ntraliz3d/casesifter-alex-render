"""Per-thread AI review completeness for a Gemini stage.

``_post_gemini`` records ultimate call failures (after retries / non-retryable
codes). ``run_gemini_stage`` reads the result into the report and job row so a
partial Gemini failure cannot look like a clean complete review.
"""

from __future__ import annotations

import threading
from typing import Literal

AiReviewReason = Literal["rate_limited", "billing", "error", "file_not_sent"]

_PRIORITY: dict[str, int] = {
    "billing": 4,
    "rate_limited": 3,
    "file_not_sent": 2,
    "error": 1,
}
_tls = threading.local()


def reset_ai_review_state() -> None:
    _tls.reason = None


def note_ai_review_failure(reason: AiReviewReason | str) -> None:
    """Record a failure; keep the highest-priority reason if several occur."""
    key = str(reason or "error")
    if key not in _PRIORITY:
        key = "error"
    current = getattr(_tls, "reason", None)
    if current is None or _PRIORITY[key] > _PRIORITY.get(current, 0):
        _tls.reason = key


def get_ai_review_failure() -> str | None:
    return getattr(_tls, "reason", None)


def reason_for_http_status(code: int) -> AiReviewReason:
    if int(code) == 429:
        return "rate_limited"
    if int(code) in (402, 403):
        return "billing"
    return "error"
