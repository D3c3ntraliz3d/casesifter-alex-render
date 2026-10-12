"""Background one-at-a-time AI fact-extract backfill for failed / legacy docs.

Never runs inside another document's scan path. Deduplicates by document id.
Failed docs are retried once (extraction_retry_done=True afterward).
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass
from typing import Any, Literal

from app.extraction_status import (
    STATUS_FAILED,
    derive_extraction_status,
    needs_extraction_backfill,
)

logger = logging.getLogger(__name__)

Scope = Literal["vault", "matter"]


@dataclass(frozen=True)
class _Job:
    scope: Scope
    document_id: str
    case_id: str | None = None
    firm_id: str | None = None
    matter_id: str | None = None


_lock = threading.Lock()
_pending: deque[_Job] = deque()
_queued_ids: set[str] = set()
_worker_started = False
_wake = threading.Event()


def ensure_extract_backfill_worker() -> None:
    global _worker_started
    with _lock:
        if _worker_started:
            return
        _worker_started = True
        threading.Thread(
            target=_loop, daemon=True, name="fact-extract-backfill"
        ).start()


def enqueue_extraction_backfill(
    snaps: list[dict[str, Any]],
    *,
    scope: Scope,
    case_id: str | None = None,
    firm_id: str | None = None,
    matter_id: str | None = None,
) -> int:
    """Queue docs that need a one-shot AI extract. Returns newly queued count."""
    ensure_extract_backfill_worker()
    added = 0
    with _lock:
        for snap in snaps or []:
            if not needs_extraction_backfill(snap):
                continue
            doc_id = str(snap.get("document_id") or "").strip()
            if not doc_id or doc_id in _queued_ids:
                continue
            if not str(snap.get("text_excerpt") or "").strip():
                continue
            job = _Job(
                scope=scope,
                document_id=doc_id,
                case_id=case_id,
                firm_id=firm_id,
                matter_id=matter_id,
            )
            _pending.append(job)
            _queued_ids.add(doc_id)
            added += 1
    if added:
        _wake.set()
        logger.info(
            "fact_extract_backfill queued=%s scope=%s pending=%s",
            added,
            scope,
            len(_pending),
        )
    return added


def _loop() -> None:
    while True:
        _wake.wait(timeout=30.0)
        _wake.clear()
        while True:
            with _lock:
                if not _pending:
                    break
                job = _pending.popleft()
            try:
                _process(job)
            except Exception:
                logger.exception(
                    "fact_extract_backfill failed doc=%s", job.document_id
                )
            finally:
                with _lock:
                    _queued_ids.discard(job.document_id)
            # One document at a time; brief pause so scans stay prioritized.
            time.sleep(0.25)


def _process(job: _Job) -> None:
    if job.scope == "vault":
        _process_vault(job)
    else:
        _process_matter(job)


def _run_extract(text: str, *, label: str, doc_kind: str | None) -> tuple[list[dict], dict]:
    from app.fact_extract import extract_facts

    usage: dict[str, Any] = {}
    facts = extract_facts(
        text,
        document_label=label,
        enable_ai=True,
        doc_kind=doc_kind,
        ai_usage_out=usage,
    )
    status, err = derive_extraction_status(usage, enable_ai=True)
    if status is None:
        status = STATUS_FAILED
        err = err or "unknown"
    usage["extraction_status"] = status
    usage["extraction_error"] = err
    return facts, usage


def _process_vault(job: _Job) -> None:
    from app import cases_db

    if not job.case_id:
        return
    snap = cases_db.get_scan_snapshot(job.document_id)
    if not snap or not needs_extraction_backfill(snap):
        return
    text = str(snap.get("text_excerpt") or "").strip()
    if not text:
        cases_db.update_snapshot_extraction(
            job.document_id,
            extraction_status=STATUS_FAILED,
            extraction_error="empty_text",
            extraction_retry_done=True,
        )
        return
    label = snap.get("display_name") or snap.get("original_filename") or job.document_id
    facts, usage = _run_extract(
        text, label=str(label), doc_kind=snap.get("doc_kind")
    )
    # Merge: keep any existing stored facts that AI missed (local keys).
    existing = list(snap.get("facts") or [])
    seen = {
        (str(f.get("key")), str(f.get("entity_key") or ""), str(f.get("display")))
        for f in facts
        if isinstance(f, dict)
    }
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        token = (str(rec.get("key")), str(rec.get("entity_key") or ""), str(rec.get("display")))
        if token not in seen:
            facts.append(rec)
            seen.add(token)
    cases_db.update_snapshot_extraction(
        job.document_id,
        facts=facts,
        extraction_status=usage.get("extraction_status"),
        extraction_error=usage.get("extraction_error"),
        extraction_retry_done=True,
    )
    logger.info(
        "fact_extract_backfill vault doc=%s status=%s facts=%s calls=%s",
        job.document_id,
        usage.get("extraction_status"),
        len(facts),
        usage.get("calls"),
    )


def _process_matter(job: _Job) -> None:
    from app.fact_memory.store import get_store

    if not job.firm_id or not job.matter_id:
        return
    store = get_store()
    snaps = store.list_prior_snapshots(job.firm_id, job.matter_id)
    snap = next(
        (s for s in snaps if str(s.get("document_id")) == job.document_id),
        None,
    )
    if not snap or not needs_extraction_backfill(snap):
        return
    text = str(snap.get("text_excerpt") or "").strip()
    if not text:
        store.update_document_extraction(
            job.firm_id,
            job.matter_id,
            job.document_id,
            extraction_status=STATUS_FAILED,
            extraction_error="empty_text",
            extraction_retry_done=True,
        )
        return
    label = snap.get("display_name") or snap.get("original_filename") or job.document_id
    facts, usage = _run_extract(
        text, label=str(label), doc_kind=snap.get("doc_kind")
    )
    existing = list(snap.get("facts") or [])
    seen = {
        (str(f.get("key")), str(f.get("entity_key") or ""), str(f.get("display")))
        for f in facts
        if isinstance(f, dict)
    }
    for rec in existing:
        if not isinstance(rec, dict):
            continue
        token = (str(rec.get("key")), str(rec.get("entity_key") or ""), str(rec.get("display")))
        if token not in seen:
            facts.append(rec)
            seen.add(token)
    store.update_document_extraction(
        job.firm_id,
        job.matter_id,
        job.document_id,
        facts=facts,
        extraction_status=usage.get("extraction_status"),
        extraction_error=usage.get("extraction_error"),
        extraction_retry_done=True,
    )
    logger.info(
        "fact_extract_backfill matter doc=%s status=%s facts=%s calls=%s",
        job.document_id,
        usage.get("extraction_status"),
        len(facts),
        usage.get("calls"),
    )
