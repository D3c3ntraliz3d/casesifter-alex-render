"""Background one-at-a-time AI fact-extract backfill for failed / legacy docs.

Yields to live scans: runs only when no jobs are queued/running/local_done,
pauses when a scan starts, processes one document at a time. Never runs inside
another document's scan path. Deduplicates by document id. Failed docs are
retried once (extraction_retry_done=True afterward). When backfill finishes for
priors a scan was waiting on, compare_to_case is re-run and the report updated.
"""

from __future__ import annotations

import logging
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Literal

from app.extraction_status import (
    STATUS_FAILED,
    derive_extraction_status,
    incomplete_prior_snapshots,
    needs_extraction_backfill,
    priors_pending_message,
)

logger = logging.getLogger(__name__)

Scope = Literal["vault", "matter"]
_ACTIVE_SCAN_STATUSES = ("queued", "running", "local_done")


@dataclass(frozen=True)
class _Job:
    scope: Scope
    document_id: str
    case_id: str | None = None
    firm_id: str | None = None
    matter_id: str | None = None


@dataclass
class _RefreshWait:
    scope: Scope
    anchor_document_id: str
    waiting_prior_ids: set[str]
    job_id: str | None = None
    case_id: str | None = None
    firm_id: str | None = None
    matter_id: str | None = None
    new_facts: list[dict[str, Any]] = field(default_factory=list)
    new_entities: dict[str, Any] = field(default_factory=dict)
    new_display_name: str = ""
    new_doc_kind: str | None = None
    new_fingerprint: str | None = None
    new_sha256: str | None = None
    new_text: str = ""
    new_page_offsets: list[dict[str, int]] | None = None


_lock = threading.Lock()
_pending: deque[_Job] = deque()
_queued_ids: set[str] = set()
_refresh_waits: list[_RefreshWait] = []
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


def live_scans_busy() -> bool:
    """True when any scan job is queued, running, or local_done."""
    try:
        from app.fact_memory.store import get_store

        active = get_store().list_analysis_jobs_by_status(list(_ACTIVE_SCAN_STATUSES))
        if active:
            return True
    except Exception:
        pass
    try:
        from app.config import settings
        from app.storage import load_progress

        root = settings.jobs_dir
        if not root.exists():
            return False
        for child in root.iterdir():
            if not child.is_dir() or child.name.startswith("_"):
                continue
            progress = load_progress(child.name)
            if progress is not None and progress.status in _ACTIVE_SCAN_STATUSES:
                return True
    except Exception:
        pass
    return False


def enqueue_extraction_backfill(
    snaps: list[dict[str, Any]],
    *,
    scope: Scope,
    case_id: str | None = None,
    firm_id: str | None = None,
    matter_id: str | None = None,
) -> list[str]:
    """Queue docs that need a one-shot AI extract. Returns newly queued document ids."""
    ensure_extract_backfill_worker()
    added_ids: list[str] = []
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
            added_ids.append(doc_id)
    if added_ids:
        _wake.set()
        logger.info(
            "fact_extract_backfill queued=%s scope=%s pending=%s",
            len(added_ids),
            scope,
            len(_pending),
        )
    return added_ids


def register_compare_refresh_after_backfill(
    *,
    scope: Scope,
    anchor_document_id: str,
    waiting_prior_ids: set[str] | list[str],
    job_id: str | None = None,
    case_id: str | None = None,
    firm_id: str | None = None,
    matter_id: str | None = None,
    new_facts: list[dict[str, Any]] | None = None,
    new_entities: dict[str, Any] | None = None,
    new_display_name: str = "",
    new_doc_kind: str | None = None,
    new_fingerprint: str | None = None,
    new_sha256: str | None = None,
    new_text: str = "",
    new_page_offsets: list[dict[str, int]] | None = None,
) -> None:
    """After listed priors finish backfill, re-run compare_to_case for the anchor doc."""
    waiting = {str(x) for x in (waiting_prior_ids or []) if str(x).strip()}
    if not waiting or not anchor_document_id:
        return
    ensure_extract_backfill_worker()
    wait = _RefreshWait(
        scope=scope,
        anchor_document_id=str(anchor_document_id),
        waiting_prior_ids=waiting,
        job_id=job_id,
        case_id=case_id,
        firm_id=firm_id,
        matter_id=matter_id,
        new_facts=list(new_facts or []),
        new_entities=dict(new_entities or {}),
        new_display_name=new_display_name or "",
        new_doc_kind=new_doc_kind,
        new_fingerprint=new_fingerprint,
        new_sha256=new_sha256,
        new_text=new_text or "",
        new_page_offsets=list(new_page_offsets or []) if new_page_offsets else None,
    )
    with _lock:
        _refresh_waits.append(wait)
    _wake.set()


def _loop() -> None:
    while True:
        _wake.wait(timeout=15.0)
        _wake.clear()
        while True:
            if live_scans_busy():
                logger.info("fact_extract_backfill paused: live scan queued/running")
                time.sleep(2.0)
                break
            with _lock:
                if not _pending:
                    break
                job = _pending.popleft()
            try:
                if live_scans_busy():
                    # Scan started while we were waiting on the lock — re-queue.
                    with _lock:
                        _pending.appendleft(job)
                    break
                _process(job)
                _on_prior_backfill_done(job.document_id)
            except Exception:
                logger.exception(
                    "fact_extract_backfill failed doc=%s", job.document_id
                )
                _on_prior_backfill_done(job.document_id)
            finally:
                with _lock:
                    _queued_ids.discard(job.document_id)
            # One document at a time; yield so a newly queued scan can preempt.
            time.sleep(0.25)
            if live_scans_busy():
                break


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


def _merge_facts(existing: list, extracted: list) -> list[dict[str, Any]]:
    facts = [f for f in extracted if isinstance(f, dict)]
    seen = {
        (str(f.get("key")), str(f.get("entity_key") or ""), str(f.get("display")))
        for f in facts
    }
    for rec in existing or []:
        if not isinstance(rec, dict):
            continue
        token = (str(rec.get("key")), str(rec.get("entity_key") or ""), str(rec.get("display")))
        if token not in seen:
            facts.append(rec)
            seen.add(token)
    return facts


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
    extracted, usage = _run_extract(
        text, label=str(label), doc_kind=snap.get("doc_kind")
    )
    facts = _merge_facts(list(snap.get("facts") or []), extracted)
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
    extracted, usage = _run_extract(
        text, label=str(label), doc_kind=snap.get("doc_kind")
    )
    facts = _merge_facts(list(snap.get("facts") or []), extracted)
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


def _on_prior_backfill_done(prior_document_id: str) -> None:
    ready: list[_RefreshWait] = []
    with _lock:
        still: list[_RefreshWait] = []
        for wait in _refresh_waits:
            wait.waiting_prior_ids.discard(prior_document_id)
            if wait.waiting_prior_ids:
                still.append(wait)
            else:
                ready.append(wait)
        _refresh_waits[:] = still
    for wait in ready:
        try:
            _run_compare_refresh(wait)
        except Exception:
            logger.exception(
                "fact_extract_backfill compare refresh failed anchor=%s",
                wait.anchor_document_id,
            )


def _run_compare_refresh(wait: _RefreshWait) -> None:
    from app.case_consistency import compare_to_case
    from app.models import Finding, Report

    if wait.scope == "vault":
        from app import cases_db

        if not wait.case_id:
            return
        priors = cases_db.list_prior_snapshots(
            wait.case_id, exclude_document_id=wait.anchor_document_id
        )
        still = incomplete_prior_snapshots(priors)
        snap = cases_db.get_scan_snapshot(wait.anchor_document_id) or {}
        facts = wait.new_facts or list(snap.get("facts") or [])
        entities = wait.new_entities or dict(snap.get("entities") or {})
        text = wait.new_text or str(snap.get("text_excerpt") or "")
        consistency, fact_matrix = compare_to_case(
            new_document_id=wait.anchor_document_id,
            new_display_name=wait.new_display_name
            or snap.get("display_name")
            or "document",
            new_entities=entities,
            new_amounts=[],
            new_fingerprint=wait.new_fingerprint or snap.get("text_fingerprint"),
            prior_snapshots=priors,
            new_doc_kind=wait.new_doc_kind or snap.get("doc_kind"),
            new_facts=facts,
            new_text=text,
            case_documents=cases_db.list_documents(wait.case_id),
            new_sha256=wait.new_sha256 or snap.get("sha256"),
            new_page_offsets=wait.new_page_offsets or snap.get("page_offsets"),
        )
        pending_n = len(still)
        _patch_job_report(
            wait.job_id,
            consistency=consistency,
            fact_matrix=fact_matrix,
            prior_count=len(priors),
            pending_n=pending_n,
            facts=facts,
        )
        try:
            cases_db.elevate_documents_for_fact_conflicts(wait.case_id)
        except Exception:
            pass
        logger.info(
            "fact_extract_backfill vault compare refresh anchor=%s priors=%s pending=%s alerts=%s",
            wait.anchor_document_id,
            len(priors),
            pending_n,
            len(consistency),
        )
        return

    # matter / fact-memory
    from app.fact_memory.store import get_store

    if not wait.firm_id or not wait.matter_id:
        return
    store = get_store()
    priors = store.list_prior_snapshots(
        wait.firm_id,
        wait.matter_id,
        exclude_document_id=wait.anchor_document_id,
    )
    still = incomplete_prior_snapshots(priors)
    report_dict = store.get_document_report(
        wait.firm_id, wait.matter_id, wait.anchor_document_id
    )
    if not isinstance(report_dict, dict):
        return
    comparison = dict(report_dict.get("comparison") or {})
    facts = wait.new_facts or list(comparison.get("facts") or [])
    entities = wait.new_entities or dict(comparison.get("entities") or {})
    text = wait.new_text or str(comparison.get("text_excerpt") or "")
    consistency, fact_matrix = compare_to_case(
        new_document_id=wait.anchor_document_id,
        new_display_name=wait.new_display_name or "document",
        new_entities=entities,
        new_amounts=[],
        new_fingerprint=wait.new_fingerprint or comparison.get("text_fingerprint"),
        prior_snapshots=priors,
        new_doc_kind=wait.new_doc_kind or comparison.get("doc_kind"),
        new_facts=facts,
        new_text=text,
        new_sha256=wait.new_sha256,
        new_page_offsets=wait.new_page_offsets or comparison.get("page_offsets"),
    )
    pending_n = len(still)
    msg = priors_pending_message(pending_n)
    cc = dict(comparison.get("case_consistency") or {})
    cc.update(
        {
            "prior_documents": len(priors),
            "alerts": len(consistency),
            "facts": facts,
            "fact_matrix": fact_matrix,
            "priors_pending_extraction": pending_n,
            "priors_pending_message": msg,
            "compare_refreshed_after_backfill": True,
        }
    )
    comparison["case_consistency"] = cc
    comparison["fact_matrix"] = fact_matrix
    comparison["priors_pending_extraction"] = pending_n
    comparison["priors_pending_message"] = msg
    report_dict["comparison"] = comparison

    # Replace non-gotcha case_consistency findings with the refreshed set.
    old_findings = list(report_dict.get("findings") or [])
    kept: list[dict[str, Any]] = []
    for item in old_findings:
        if not isinstance(item, dict):
            continue
        ev = item.get("evidence") or {}
        if item.get("category") == "case_consistency" and ev.get("case_consistency"):
            if ev.get("gotcha") or item.get("gotcha") or ev.get("analysis_type") == "gotcha":
                kept.append(item)
            continue
        kept.append(item)
    kept.extend([f.model_dump(mode="json") for f in consistency])
    report_dict["findings"] = kept
    store.update_document_report(
        wait.firm_id, wait.matter_id, wait.anchor_document_id, report_dict
    )
    _patch_job_report(
        wait.job_id,
        consistency=consistency,
        fact_matrix=fact_matrix,
        prior_count=len(priors),
        pending_n=pending_n,
        facts=facts,
    )
    logger.info(
        "fact_extract_backfill matter compare refresh anchor=%s priors=%s pending=%s alerts=%s",
        wait.anchor_document_id,
        len(priors),
        pending_n,
        len(consistency),
    )


def _patch_job_report(
    job_id: str | None,
    *,
    consistency: list,
    fact_matrix: list,
    prior_count: int,
    pending_n: int,
    facts: list,
) -> None:
    if not job_id:
        return
    try:
        from app.models import Finding, Report
        from app.storage import load_report, update_job

        report = load_report(job_id)
        if report is None:
            return
        msg = priors_pending_message(pending_n)
        comparison = dict(report.comparison or {})
        cc = dict(comparison.get("case_consistency") or {})
        cc.update(
            {
                "prior_documents": prior_count,
                "alerts": len(consistency),
                "facts": facts,
                "fact_matrix": fact_matrix,
                "priors_pending_extraction": pending_n,
                "priors_pending_message": msg,
                "compare_refreshed_after_backfill": True,
            }
        )
        comparison["case_consistency"] = cc
        comparison["fact_matrix"] = fact_matrix
        comparison["priors_pending_extraction"] = pending_n
        comparison["priors_pending_message"] = msg

        kept: list[Finding] = []
        for item in report.findings or []:
            ev = item.evidence or {}
            if item.category == "case_consistency" and ev.get("case_consistency"):
                if ev.get("gotcha") or getattr(item, "gotcha", None) or ev.get("analysis_type") == "gotcha":
                    kept.append(item)
                continue
            kept.append(item)
        kept.extend(list(consistency))
        updated = report.model_copy(
            update={"findings": kept, "comparison": comparison}
        )
        update_job(job_id, report=updated)
    except Exception:
        logger.exception("fact_extract_backfill job report patch failed job=%s", job_id)
