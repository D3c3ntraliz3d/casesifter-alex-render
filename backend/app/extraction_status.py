"""Per-document AI fact-extraction status.

An empty facts list with status extracted_ok or skipped_boilerplate must never
trigger another Gemini extract. Failed docs may be retried once by the
background backfill worker — never inline inside another document's scan.
"""

from __future__ import annotations

from typing import Any

STATUS_EXTRACTED_OK = "extracted_ok"
STATUS_SKIPPED_BOILERPLATE = "skipped_boilerplate"
STATUS_FAILED = "failed"

TERMINAL_NO_RETRY = frozenset({STATUS_EXTRACTED_OK, STATUS_SKIPPED_BOILERPLATE})


def derive_extraction_status(
    usage: dict[str, Any] | None,
    *,
    enable_ai: bool,
) -> tuple[str | None, str | None]:
    """Return (status, error_reason) from an ai_fact_extract usage dict.

    When ``enable_ai`` is False (local/regex-only), returns (None, None) so
    callers do not overwrite a prior AI status.
    """
    if not enable_ai:
        return None, None
    usage = usage or {}
    skipped = str(usage.get("skipped") or "").strip()
    err = str(usage.get("error") or "").strip()

    if skipped.startswith("boilerplate") or skipped in {
        "court_filing",
        "boilerplate_filename",
    }:
        return STATUS_SKIPPED_BOILERPLATE, skipped or None

    if skipped in {"gemini_error", "exception", "import_error"} or err:
        return STATUS_FAILED, (err or skipped)[:300] or "error"

    # Completed path: Gemini ran, or a hard no-op that must not be re-attempted
    # (empty text / no API key). Zero facts still count as extracted_ok.
    return STATUS_EXTRACTED_OK, skipped[:300] if skipped else None


def needs_extraction_backfill(snap: dict[str, Any] | None) -> bool:
    """True when a separate background worker should attempt AI extract once."""
    if not isinstance(snap, dict):
        return False
    status = str(snap.get("extraction_status") or "").strip()
    retry_done = bool(snap.get("extraction_retry_done"))
    if status in TERMINAL_NO_RETRY:
        return False
    if retry_done:
        return False
    if status == STATUS_FAILED:
        return True
    # Legacy snapshots with no status: one background fill so we persist a status.
    if not status:
        text = str(snap.get("text_excerpt") or "").strip()
        return bool(text)
    return False


def incomplete_prior_snapshots(priors: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Priors still pending/failed extraction — not yet fully in cross-doc compare."""
    out: list[dict[str, Any]] = []
    for snap in priors or []:
        if needs_extraction_backfill(snap):
            out.append(snap)
    return out


def priors_pending_message(count: int) -> str | None:
    n = int(count or 0)
    if n <= 0:
        return None
    noun = "document" if n == 1 else "documents"
    return f"{n} earlier {noun} not yet included in cross-document comparison"
