"""Per-thread AI review completeness for a Gemini stage.

``_post_gemini`` records ultimate call failures (after retries / non-retryable
codes). Chunked Files API analysis records ``partial`` with failed page ranges.
``run_gemini_stage`` reads the result into the report and job row so a
partial Gemini failure cannot look like a clean complete review.
"""

from __future__ import annotations

import threading
from typing import Literal

AiReviewReason = Literal[
    "rate_limited",
    "billing",
    "error",
    "file_not_sent",
    "partial",
    "no_api_key",
    "multimodal_off",
    "not_attempted",
    "empty_response",
    "timeout",
]

_PRIORITY: dict[str, int] = {
    "billing": 9,
    "rate_limited": 8,
    "partial": 7,
    "file_not_sent": 6,
    "no_api_key": 5,
    "multimodal_off": 5,
    "timeout": 4,
    "empty_response": 4,
    "not_attempted": 3,
    "error": 1,
}
_tls = threading.local()


def reset_ai_review_state() -> None:
    _tls.reason = None
    _tls.failed_page_ranges = None


def note_ai_review_failure(reason: AiReviewReason | str) -> None:
    """Record a failure; keep the highest-priority reason if several occur."""
    key = str(reason or "error")
    if key not in _PRIORITY:
        key = "error"
    current = getattr(_tls, "reason", None)
    if current is None or _PRIORITY[key] > _PRIORITY.get(current, 0):
        _tls.reason = key


def note_partial_failure(page_ranges: list[tuple[int, int]] | list[list[int]]) -> None:
    """Mark AI review incomplete because one or more chunks failed."""
    note_ai_review_failure("partial")
    cleaned: list[list[int]] = []
    for item in page_ranges or []:
        if not item or len(item) < 2:
            continue
        start, end = int(item[0]), int(item[1])
        if start < 1 or end < start:
            continue
        cleaned.append([start, end])
    existing = list(getattr(_tls, "failed_page_ranges", None) or [])
    existing.extend(cleaned)
    _tls.failed_page_ranges = existing


def get_ai_review_failure() -> str | None:
    return getattr(_tls, "reason", None)


def get_failed_page_ranges() -> list[list[int]]:
    return list(getattr(_tls, "failed_page_ranges", None) or [])


def reason_for_http_status(code: int) -> AiReviewReason:
    if int(code) == 429:
        return "rate_limited"
    if int(code) in (402, 403):
        return "billing"
    return "error"
