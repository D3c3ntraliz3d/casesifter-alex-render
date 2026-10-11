from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app import __version__
from app import cases_db
from app.case_consistency import (
    compare_to_case,
    excerpt_hashes,
    extract_amounts,
    findings_summary,
    text_fingerprint,
)
from app.fact_extract import extract_facts
from app.gotcha import find_gotchas
from app.config import settings
from app.engines.contextual_logic import analyze_contextual
from app.engines.image_forensics import analyze_image_forensics
from app.engines.metadata import analyze_metadata
from app.engines.ocr_text import analyze_ocr_text
from app.engines.pdf_integrity import analyze_pdf_integrity, pdf_origin_guess
from app.engines.structural import analyze_structural
from app.ingestion import sha256_file, sniff_type
from app.models import AuditTrail, Finding, FontUsage, ModuleResult, PageDimension, Report, TimelineEvent
from app.engines.statement_dates import find_statement_date_conflicts
from app.engines.template_fabrication import find_template_leftovers, review_print_scan_ela
from app.finding_policy import triage_findings
from app.impact_judge import apply_case_impact_judge
from app.report import (
    executive_summary,
    pick_top_findings,
    recommended_actions,
    render_html,
    render_pdf,
    score_risk_details,
)
from app.scan_profile import (
    PROFILE_COURT_FILING,
    finalize_scan_profile,
    peek_pdf_text,
    resolve_scan_profile,
)
from app.storage import job_dir, load_progress, update_job
from app.product_events import emit_scan_completed, emit_scan_started, finding_count_summary
from app.telemetry import emit_scan_outcomes
from app import gemini_timing
from app.resource import rss_mb

logger = logging.getLogger("uvicorn.error")

LOCAL_STAGE_NAME = "local_stage.json"


def _source_for_contextual(source: Path, profile: str) -> Path | None:
    """Native file upload for tier-3 Gemini fraud review — forensic lane only."""
    if profile == PROFILE_COURT_FILING:
        return None
    if not (settings.contextual_multimodal and settings.gemini_api_key):
        return None
    try:
        # Match contextual_logic inline limit (default 15 MB), not a tighter hardcode.
        limit = settings.contextual_max_upload_mb * 1024 * 1024
        if source.stat().st_size > limit:
            return None
    except OSError:
        return None
    return source


def _skipped_module(name: str, reason: str) -> ModuleResult:
    return ModuleResult(
        name=name,
        status="skipped",
        duration_ms=0,
        summary=reason,
        findings=[],
        extras={"skipped": True, "reason": reason},
    )


def _local_stage_path(job_id: str) -> Path:
    return job_dir(job_id) / LOCAL_STAGE_NAME


def _dump_module(module: ModuleResult) -> dict[str, Any]:
    return module.model_dump(mode="json")


def _load_module(data: dict[str, Any]) -> ModuleResult:
    return ModuleResult.model_validate(data)


def _write_timing(job_id: str, timing: dict[str, Any], *, peak_rss_mb: float | None = None) -> None:
    progress = load_progress(job_id)
    if progress is None:
        return
    payload = dict(timing)
    if peak_rss_mb is not None:
        payload["peak_rss_mb"] = round(float(peak_rss_mb), 1)
    web = rss_mb()
    if web is not None:
        payload["web_rss_mb"] = web
    # Stash on progress via message-adjacent JSON file (JobProgress has no timing field).
    (job_dir(job_id) / "timing.json").write_text(json.dumps(payload, indent=2), encoding="utf-8")
    # Also mirror into progress message-friendly fields by rewriting progress.json extras.
    raw_path = job_dir(job_id) / "progress.json"
    try:
        raw = json.loads(raw_path.read_text(encoding="utf-8"))
        raw["timing"] = payload
        raw_path.write_text(json.dumps(raw, indent=2, default=str), encoding="utf-8")
    except Exception:
        logger.debug("timing progress mirror failed job=%s", job_id[:8], exc_info=True)
    logger.info(
        "scan timing job=%s local_s=%s gemini_s=%s other_s=%s total_s=%s peak_rss_mb=%s web_rss_mb=%s",
        job_id[:8],
        payload.get("local_s"),
        payload.get("gemini_s"),
        payload.get("other_s"),
        payload.get("total_s"),
        payload.get("peak_rss_mb"),
        payload.get("web_rss_mb"),
    )


def run_local_stage(
    job_id: str,
    source: Path,
    submitted_by: str = "court-staff",
    *,
    case_id: str | None = None,
    document_id: str | None = None,
    display_name: str | None = None,
    doc_kind: str | None = None,
    prior_snapshots: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """PDF/OCR/local extract only. Writes local_stage.json; leaves source on disk."""
    started = time.monotonic()
    size_bytes = None
    try:
        size_bytes = source.stat().st_size
    except OSError:
        pass
    suffix = source.suffix.lower()
    peek_text = ""
    if not (doc_kind or "").strip():
        if suffix == ".pdf":
            peek_text = peek_pdf_text(source)
        elif suffix in {".txt", ".text"}:
            try:
                peek_text = source.read_text(encoding="utf-8", errors="ignore")[:8000]
            except OSError:
                peek_text = ""
    profile = resolve_scan_profile(doc_kind, peek_text)
    origin_for_profile: dict[str, Any] = {}
    if suffix == ".pdf":
        origin_for_profile = pdf_origin_guess(source)
    profile = finalize_scan_profile(
        profile,
        doc_kind=doc_kind,
        peek_text=peek_text,
        suffix=suffix,
        origin=origin_for_profile,
        filename=source.name,
        display_name=display_name or "",
    )
    court_filing = profile == PROFILE_COURT_FILING

    emit_scan_started(job_id=job_id, doc_kind=doc_kind, size_bytes=size_bytes)
    update_job(job_id, status="running", stage="ingesting", percent=5, message="Reading file and hashing contents…")
    if document_id:
        cases_db.update_document(document_id, status="scanning", scan_job_id=job_id)

    file_type = sniff_type(source)
    digest = sha256_file(source)
    size = source.stat().st_size
    if document_id:
        cases_db.update_document(document_id, sha256=digest)

    update_job(job_id, stage="metadata", percent=15, message="Extracting timestamps and authorship…")
    metadata = analyze_metadata(source)

    if court_filing:
        update_job(
            job_id,
            stage="pdf_integrity",
            percent=30,
            message="Court filing profile — skipping exhibit forgery structure checks…",
        )
        pdf = _skipped_module(
            "pdf_integrity",
            "Skipped for court filings (complaints/motions). Use exhibit profile for AcroForm / JS checks.",
        )
        structural = _skipped_module(
            "structural",
            "Skipped for court filings. Mixed page size/rotation is normal in e-filed pleadings.",
        )
        images = _skipped_module(
            "image_forensics",
            "Skipped for court filings. Whiteout / ELA / clone detection runs on exhibits only.",
        )
    else:
        update_job(job_id, stage="pdf_integrity", percent=30, message="Checking PDF structure and hidden content…")
        pdf = analyze_pdf_integrity(source)

        update_job(job_id, stage="structural", percent=45, message="Comparing page sizes and orientation…")
        structural = analyze_structural(source)

        update_job(job_id, stage="image_forensics", percent=60, message="Running image manipulation detection…")
        try:
            images = analyze_image_forensics(
                source,
                job_id,
                origin=(pdf.extras or {}).get("origin_guess") or origin_for_profile or {},
            )
        except Exception as exc:
            images = ModuleResult(
                name="image_forensics",
                status="error",
                duration_ms=0,
                summary=f"Image forensics failed ({exc}); continuing with multimodal review.",
                findings=[],
                extras={"error": str(exc)[:400]},
            )

    update_job(
        job_id,
        stage="ocr_text",
        percent=78,
        message="Extracting text for case logic…" if court_filing else "Extracting text and checking fonts…",
    )
    ocr = analyze_ocr_text(source, metadata.extras, scan_profile=profile)
    full_text = ocr.extras.pop("full_text", "") or ""
    page_offsets = list(ocr.extras.get("page_offsets") or [])

    local_findings: list[Finding] = []
    if not court_filing:
        local_findings.extend(
            find_template_leftovers(
                full_text,
                scan_profile=profile,
                source=display_name or source.name,
                doc_kind=doc_kind,
            )
        )
        local_findings.extend(
            find_statement_date_conflicts(
                full_text,
                scan_profile=profile,
                source=display_name or source.name,
                doc_kind=doc_kind,
            )
        )
        # ELA review needs image findings; apply after modules merge in gemini stage.
        pass

    local_facts = extract_facts(
        full_text,
        page_offsets=page_offsets,
        document_label=display_name or source.name,
        enable_ai=False,
    )
    amounts = extract_amounts(full_text)
    fingerprint = text_fingerprint(full_text)
    hashes = excerpt_hashes(full_text)
    local_s = round(time.monotonic() - started, 2)

    payload = {
        "job_id": job_id,
        "source_name": source.name,
        "submitted_by": submitted_by,
        "case_id": case_id,
        "document_id": document_id,
        "display_name": display_name,
        "doc_kind": doc_kind,
        "prior_snapshots": prior_snapshots or [],
        "profile": profile,
        "court_filing": court_filing,
        "file_type": file_type,
        "digest": digest,
        "size": size,
        "origin_for_profile": origin_for_profile,
        "metadata": _dump_module(metadata),
        "pdf": _dump_module(pdf),
        "structural": _dump_module(structural),
        "images": _dump_module(images),
        "ocr": _dump_module(ocr),
        "full_text": full_text,
        "page_offsets": page_offsets,
        "local_findings": [f.model_dump(mode="json") for f in local_findings],
        "local_facts": local_facts,
        "amounts": amounts,
        "fingerprint": fingerprint,
        "hashes": hashes,
        "local_s": local_s,
    }
    _local_stage_path(job_id).write_text(json.dumps(payload, default=str), encoding="utf-8")
    update_job(
        job_id,
        status="local_done",
        stage="local_done",
        percent=82,
        message="Local PDF/OCR complete — queued for Gemini review…",
    )
    return {"job_id": job_id, "status": "local_done", "local_s": local_s, "digest": digest}


def run_gemini_stage(
    job_id: str,
    source: Path,
    *,
    peak_rss_mb: float | None = None,
) -> Report:
    """Multimodal / claims / AI facts / gotchas / report. Reads local_stage.json."""
    from app.gemini_review_state import get_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    gemini_timing.reset()
    gemini_started = time.monotonic()
    stage_path = _local_stage_path(job_id)
    if not stage_path.is_file():
        raise FileNotFoundError(f"Missing local stage artifacts for job {job_id}")
    data = json.loads(stage_path.read_text(encoding="utf-8"))

    submitted_by = data.get("submitted_by") or "court-staff"
    case_id = data.get("case_id")
    document_id = data.get("document_id")
    display_name = data.get("display_name")
    doc_kind = data.get("doc_kind")
    prior_snapshots = data.get("prior_snapshots") or []
    profile = data["profile"]
    court_filing = bool(data.get("court_filing"))
    file_type = data["file_type"]
    digest = data["digest"]
    size = int(data["size"])
    metadata = _load_module(data["metadata"])
    pdf = _load_module(data["pdf"])
    structural = _load_module(data["structural"])
    images = _load_module(data["images"])
    ocr = _load_module(data["ocr"])
    full_text = data.get("full_text") or ""
    page_offsets = list(data.get("page_offsets") or [])
    local_s = float(data.get("local_s") or 0.0)

    update_job(
        job_id,
        status="running",
        stage="contextual",
        percent=88,
        message="Extracting parties, dates, and claims for case comparison…"
        if court_filing
        else "Checking entities and exhibit fraud indicators…",
    )

    doc_meta = dict((metadata.extras.get("metadata") or {}).get("document") or {})
    doc_meta["doc_kind"] = doc_kind
    doc_meta["filename"] = display_name or source.name
    contextual = analyze_contextual(
        full_text,
        doc_meta,
        source_file=_source_for_contextual(source, profile),
        scan_profile=profile,
    )
    # Heartbeat only after a real sub-step completes (no timer ticker).
    update_job(
        job_id,
        stage="generating_report",
        percent=90,
        message="Contextual review complete — extracting facts…",
    )

    modules = [metadata, pdf, structural, images, ocr, contextual]
    findings: list[Finding] = []
    for module in modules:
        findings.extend(module.findings)
    for item in data.get("local_findings") or []:
        findings.append(Finding.model_validate(item))
    if not court_filing:
        findings = review_print_scan_ela(findings, full_text, scan_profile=profile)

    entities = (contextual.extras or {}).get("entities") or {}
    amounts = data.get("amounts") or extract_amounts(full_text)
    ai_fact_usage: dict[str, Any] = {}
    facts = list(data.get("local_facts") or [])
    # Merge AI facts on top of local regex facts.
    facts = extract_facts(
        full_text,
        page_offsets=page_offsets,
        ai_usage_out=ai_fact_usage,
        document_label=display_name or source.name,
        enable_ai=True,
    )
    update_job(
        job_id,
        stage="generating_report",
        percent=94,
        message="Fact extract complete — compiling the authenticity report…",
    )
    # Prefer union: keep local-only keys that AI missed.
    seen = {
        (str(f.get("key")), str(f.get("entity_key") or ""), str(f.get("display")))
        for f in facts
    }
    for rec in data.get("local_facts") or []:
        token = (str(rec.get("key")), str(rec.get("entity_key") or ""), str(rec.get("display")))
        if token not in seen:
            facts.append(rec)
            seen.add(token)

    fingerprint = data.get("fingerprint") or text_fingerprint(full_text)
    hashes = data.get("hashes") or excerpt_hashes(full_text)
    fact_matrix: list[dict[str, Any]] = []
    peer_priors = [snap for snap in prior_snapshots if isinstance(snap, dict)]

    if case_id and document_id:
        update_job(
            job_id,
            stage="case_consistency",
            percent=92,
            message="Comparing labeled facts against prior documents in this case…",
        )
        if document_id and digest:
            cases_db.update_document(document_id, sha256=digest)
        cases_db.save_scan_snapshot(
            document_id=document_id,
            case_id=case_id,
            entities=entities,
            findings_summary=findings_summary(findings),
            text_fingerprint=fingerprint,
            excerpt_hashes=hashes,
            amounts=amounts,
            text_excerpt=full_text[:24000],
            facts=facts,
            page_offsets=page_offsets,
        )
        priors = cases_db.list_prior_snapshots(case_id, exclude_document_id=document_id)
        name = display_name or source.name
        consistency, fact_matrix = compare_to_case(
            new_document_id=document_id,
            new_display_name=name,
            new_entities=entities,
            new_amounts=amounts,
            new_fingerprint=fingerprint,
            prior_snapshots=priors,
            new_doc_kind=doc_kind,
            new_facts=facts,
            new_text=full_text,
            case_documents=cases_db.list_documents(case_id),
            new_sha256=digest,
            new_page_offsets=page_offsets,
        )
        findings.extend(consistency)
        update_job(
            job_id,
            stage="case_consistency",
            percent=93,
            message="Checking claims against hard evidence in this case…",
        )
        gotchas, gotcha_log = find_gotchas(
            case_id=case_id,
            document_id=document_id,
            display_name=name,
            doc_kind=doc_kind,
            text=full_text,
            current_findings=findings,
        )
        findings.extend(gotchas)
        timing_bits = []
        mm = (contextual.extras or {}).get("multimodal_ms")
        ch = (contextual.extras or {}).get("logo_chrome_ms")
        gv = (gotcha_log or {}).get("gotcha_verify_ms")
        if mm is not None:
            timing_bits.append(f"multimodal {mm}ms")
        if ch is not None:
            timing_bits.append(f"chrome {ch}ms")
        if gv is not None:
            timing_bits.append(
                f"gotcha-verify {gv}ms"
                f" ({(gotcha_log or {}).get('verify_provider', '?')}"
                f" amb={(gotcha_log or {}).get('verify_ambiguous_count', '?')}"
                f"/{(gotcha_log or {}).get('verify_candidate_count', '?')})"
            )
        if timing_bits:
            update_job(
                job_id,
                stage="case_consistency",
                percent=93,
                message="Timing: " + "; ".join(timing_bits),
            )
        contextual.extras["case_consistency"] = {
            "prior_documents": len(priors),
            "alerts": len(consistency),
            "gotchas": len(gotchas),
            "gotcha_log": gotcha_log,
            "facts": facts,
            "fact_matrix": fact_matrix,
            "multimodal_ms": mm,
            "logo_chrome_ms": ch,
            "gotcha_verify_ms": gv,
            "verify_provider": (gotcha_log or {}).get("verify_provider"),
            "verify_candidate_count": (gotcha_log or {}).get("verify_candidate_count"),
            "verify_ambiguous_count": (gotcha_log or {}).get("verify_ambiguous_count"),
        }
    elif peer_priors:
        update_job(
            job_id,
            stage="case_consistency",
            percent=92,
            message="Comparing labeled facts against other files in this desktop case…",
        )
        name = display_name or source.name
        ephemeral_id = document_id or f"ephemeral-{job_id[:12]}"
        consistency, fact_matrix = compare_to_case(
            new_document_id=ephemeral_id,
            new_display_name=name,
            new_entities=entities,
            new_amounts=amounts,
            new_fingerprint=fingerprint,
            prior_snapshots=peer_priors,
            new_doc_kind=doc_kind,
            new_facts=facts,
            new_text=full_text,
            case_documents=[
                {
                    "id": snap.get("document_id"),
                    "display_name": snap.get("display_name") or snap.get("original_filename"),
                    "original_filename": snap.get("original_filename"),
                    "doc_kind": snap.get("doc_kind"),
                    "sha256": snap.get("sha256"),
                }
                for snap in peer_priors
            ],
            new_sha256=digest,
            new_page_offsets=page_offsets,
        )
        findings.extend(consistency)
        contextual.extras["case_consistency"] = {
            "prior_documents": len(peer_priors),
            "alerts": len(consistency),
            "gotchas": 0,
            "gotcha_log": {"ephemeral_peers": True},
            "facts": facts,
            "fact_matrix": fact_matrix,
            "ephemeral_peers": True,
        }

    findings = triage_findings(findings, profile)
    findings, impact_log = apply_case_impact_judge(findings)
    update_job(
        job_id,
        stage="generating_report",
        percent=96,
        message="Triage complete — writing report…",
    )
    if isinstance(contextual.extras.get("case_consistency"), dict):
        contextual.extras["case_consistency"]["case_impact"] = impact_log

    timeline = [TimelineEvent.model_validate(item) for item in (metadata.extras.get("timeline") or [])]
    dims: list[PageDimension] = []
    for item in structural.extras.get("dimension_models") or []:
        if isinstance(item, PageDimension):
            dims.append(item)
        else:
            dims.append(PageDimension.model_validate(item))
    fonts: list[FontUsage] = ocr.extras.pop("font_models", []) or []
    if fonts and not isinstance(fonts[0], FontUsage):
        fonts = [FontUsage.model_validate(item) for item in fonts]
    structural.extras.pop("dimension_models", None)

    from app.case_consistency import timeline_context_from_facts

    comparison: dict[str, Any] = {
        "fonts": ocr.extras.get("fonts") or [],
        "page_dimensions": structural.extras.get("page_dimensions") or [],
        "origin_guess": (pdf.extras or {}).get("origin_guess"),
        "ela_artifacts": images.extras.get("artifacts") or [],
        "entities": entities,
        "external_verification": (contextual.extras or {}).get("external_verification"),
        "case_consistency": (contextual.extras or {}).get("case_consistency"),
        "gotcha_log": ((contextual.extras or {}).get("case_consistency") or {}).get("gotcha_log"),
        "facts": facts,
        "timeline_context": timeline_context_from_facts(facts),
        "fact_matrix": fact_matrix,
        "scan_profile": profile,
        "doc_kind": doc_kind,
        "case_impact": impact_log,
        "ai_fact_extract_usage": ai_fact_usage,
        "gemini_usage": {
            **((contextual.extras or {}).get("gemini_usage") or {}),
            "ai_fact_extract": ai_fact_usage,
        },
        "text_excerpt": (full_text or "")[:24000],
    }

    fail_reason = get_ai_review_failure()
    if fail_reason:
        ai_review = "incomplete"
        ai_review_reason = fail_reason
    else:
        ai_review = "complete" if settings.gemini_api_key else None
        ai_review_reason = None
    comparison["ai_review"] = ai_review
    comparison["ai_review_reason"] = ai_review_reason

    risk_level, risk_score, score_drivers = score_risk_details(findings)
    comparison["score_drivers"] = score_drivers
    top = pick_top_findings(findings)
    audit = AuditTrail(
        scanned_at=datetime.now(timezone.utc),
        analysis_version=__version__,
        modules_run=[m.name for m in modules if m.status != "skipped"],
        submitted_by=submitted_by,
        filename=source.name,
        file_size_bytes=size,
        file_type=file_type,
        sha256=digest,
    )
    artifact_urls = [
        f"/api/jobs/{job_id}/artifacts/{name}" for name in (images.extras.get("artifacts") or [])
    ]
    report_name = display_name or source.name
    report = Report(
        job_id=job_id,
        risk_level=risk_level,
        risk_score=risk_score,
        ai_review=ai_review,
        ai_review_reason=ai_review_reason,
        executive_summary=executive_summary(risk_level, report_name, findings),
        top_findings=top,
        recommended_actions=recommended_actions(risk_level, findings),
        findings=findings,
        timeline=timeline,
        page_dimensions=dims,
        fonts=fonts,
        comparison=comparison,
        modules=modules,
        audit=audit,
        artifact_urls=artifact_urls,
    )

    html = render_html(report)
    (job_dir(job_id) / "report.html").write_text(html, encoding="utf-8")
    update_job(
        job_id,
        stage="generating_report",
        percent=98,
        message="Report HTML written…",
    )
    try:
        render_pdf(report)
    except Exception:
        pass

    gemini_wall = time.monotonic() - gemini_started
    gemini_snap = gemini_timing.snapshot()
    gemini_s = float(gemini_snap.get("gemini_s") or 0.0)
    other_s = round(max(0.0, gemini_wall - gemini_s), 2)
    total_s = round(local_s + gemini_wall, 2)
    timing = {
        "local_s": round(local_s, 2),
        "gemini_s": round(gemini_s, 2),
        "other_s": other_s,
        "total_s": total_s,
        "gemini_calls": gemini_snap.get("calls"),
    }
    comparison["timing"] = timing

    if ai_review == "incomplete":
        complete_msg = (
            f"Scan finished but AI review is incomplete ({ai_review_reason or 'error'}). Re-run AI review."
        )
        try:
            (job_dir(job_id) / "retain_local_stage.flag").write_text(
                ai_review_reason or "error", encoding="utf-8"
            )
        except OSError:
            logger.debug("retain_local_stage flag write failed job=%s", job_id[:8], exc_info=True)
    else:
        complete_msg = (
            "Court-filing scan complete (content + case comparison)."
            if court_filing
            else "Exhibit forensic scan complete."
        )
        flag = job_dir(job_id) / "retain_local_stage.flag"
        if flag.exists():
            try:
                flag.unlink()
            except OSError:
                pass
    update_job(
        job_id,
        status="complete",
        stage="complete",
        percent=100,
        message=complete_msg,
        report=report,
    )
    _write_timing(job_id, timing, peak_rss_mb=peak_rss_mb)

    if document_id:
        complete_fields: dict[str, Any] = {
            "status": "complete",
            "risk_level": risk_level,
            "risk_score": risk_score,
            "scan_job_id": job_id,
            "last_scanned_at": datetime.now(timezone.utc).isoformat(),
        }
        if not (doc_kind or "").strip():
            from app.auto_title import looks_like_exhibit_filename

            fname = source.name if source else ""
            if looks_like_exhibit_filename(fname) or looks_like_exhibit_filename(display_name or ""):
                complete_fields["doc_kind"] = "exhibit"
                doc_kind = "exhibit"
        cases_db.update_document(document_id, **complete_fields)
        cases_db.save_scan_snapshot(
            document_id=document_id,
            case_id=case_id or "",
            entities=entities,
            findings_summary=findings_summary(findings),
            text_fingerprint=fingerprint,
            excerpt_hashes=hashes,
            amounts=amounts,
            text_excerpt=full_text[:24000],
            facts=facts,
        )
        if case_id:
            cases_db.elevate_documents_for_fact_conflicts(case_id)
        emit_scan_outcomes(doc_kind=doc_kind, findings=findings, risk_level=risk_level)

    emit_scan_completed(
        job_id=job_id,
        duration_ms=int(total_s * 1000),
        risk_level=risk_level,
        finding_counts=finding_count_summary(findings),
    )
    return report


def run_pipeline(
    job_id: str,
    source: Path,
    submitted_by: str = "court-staff",
    *,
    case_id: str | None = None,
    document_id: str | None = None,
    display_name: str | None = None,
    doc_kind: str | None = None,
    prior_snapshots: list[dict[str, Any]] | None = None,
) -> Report:
    """Full pipeline (vault/CLI). Local then Gemini in-process."""
    run_local_stage(
        job_id,
        source,
        submitted_by=submitted_by,
        case_id=case_id,
        document_id=document_id,
        display_name=display_name,
        doc_kind=doc_kind,
        prior_snapshots=prior_snapshots,
    )
    return run_gemini_stage(job_id, source)
