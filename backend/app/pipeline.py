from __future__ import annotations

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
from app.report import executive_summary, pick_top_findings, recommended_actions, render_html, render_pdf, score_risk
from app.scan_profile import (
    PROFILE_COURT_FILING,
    apply_media_override,
    peek_pdf_text,
    resolve_scan_profile,
)
from app.storage import job_dir, update_job
from app.product_events import emit_scan_completed, emit_scan_started, finding_count_summary
from app.telemetry import emit_scan_outcomes


def _source_for_contextual(source: Path, profile: str) -> Path | None:
    """Native PDF upload for tier-3 fraud review — exhibits only."""
    if profile == PROFILE_COURT_FILING:
        return None
    if not (settings.contextual_multimodal and settings.gemini_api_key):
        return None
    try:
        if source.stat().st_size > 8 * 1024 * 1024:
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
    started = time.monotonic()
    size_bytes = None
    try:
        size_bytes = source.stat().st_size
    except OSError:
        pass
    peek_text = peek_pdf_text(source) if not (doc_kind or "").strip() else ""
    profile = resolve_scan_profile(doc_kind, peek_text)
    # Scan always wins: images / scan-heavy PDFs stay on the forgery lane even
    # when kind or caption inference said court_filing.
    origin_for_profile: dict[str, Any] = {}
    suffix = source.suffix.lower()
    if suffix == ".pdf" and profile == PROFILE_COURT_FILING:
        origin_for_profile = pdf_origin_guess(source)
    profile = apply_media_override(profile, suffix=suffix, origin=origin_for_profile)
    court_filing = profile == PROFILE_COURT_FILING

    emit_scan_started(
        job_id=job_id,
        doc_kind=doc_kind,
        size_bytes=size_bytes,
    )
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
        images = analyze_image_forensics(
            source,
            job_id,
            origin=(pdf.extras or {}).get("origin_guess") or origin_for_profile or {},
        )

    update_job(
        job_id,
        stage="ocr_text",
        percent=78,
        message="Extracting text for case logic…" if court_filing else "Extracting text and checking fonts…",
    )
    ocr = analyze_ocr_text(source, metadata.extras, scan_profile=profile)
    full_text = ocr.extras.pop("full_text", "") or ""

    update_job(
        job_id,
        stage="contextual",
        percent=88,
        message="Extracting parties, dates, and claims for case comparison…"
        if court_filing
        else "Checking entities and exhibit fraud indicators…",
    )
    doc_meta = dict((metadata.extras.get("metadata") or {}).get("document") or {})
    # Logo/chrome and statement gates need the UI kind + display name — PDF metadata alone
    # rarely carries "bill" / "National Grid statement.png".
    doc_meta["doc_kind"] = doc_kind
    doc_meta["filename"] = display_name or source.name
    contextual = analyze_contextual(
        full_text,
        doc_meta,
        source_file=_source_for_contextual(source, profile),
        scan_profile=profile,
    )

    update_job(job_id, stage="generating_report", percent=94, message="Compiling the authenticity report…")

    modules = [metadata, pdf, structural, images, ocr, contextual]
    findings: list[Finding] = []
    for module in modules:
        findings.extend(module.findings)
    if not court_filing:
        findings.extend(
            find_template_leftovers(
                full_text,
                scan_profile=profile,
                source=display_name or source.name,
                doc_kind=doc_kind,
            )
        )
        findings.extend(
            find_statement_date_conflicts(
                full_text,
                scan_profile=profile,
                source=display_name or source.name,
                doc_kind=doc_kind,
            )
        )
        findings = review_print_scan_ela(findings, full_text, scan_profile=profile)

    entities = (contextual.extras or {}).get("entities") or {}
    amounts = extract_amounts(full_text)
    facts = extract_facts(full_text)
    fingerprint = text_fingerprint(full_text)
    hashes = excerpt_hashes(full_text)
    fact_matrix: list[dict[str, Any]] = []
    peer_priors = [snap for snap in (prior_snapshots or []) if isinstance(snap, dict)]

    if case_id and document_id:
        update_job(
            job_id,
            stage="case_consistency",
            percent=92,
            message="Comparing labeled facts against prior documents in this case…",
        )
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
        # Desktop ephemeral /v1/jobs omit case_id. Peers arrive as a client packet so
        # occupancy_start and other documentary conflicts still enter the report score.
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
                }
                for snap in peer_priors
            ],
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
        "fact_matrix": fact_matrix,
        "scan_profile": profile,
        "doc_kind": doc_kind,
        "case_impact": impact_log,
        "text_excerpt": (full_text or "")[:24000],
    }

    risk_level, risk_score = score_risk(findings)
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
    try:
        render_pdf(report)
    except Exception:
        pass

    update_job(
        job_id,
        status="complete",
        stage="complete",
        percent=100,
        message=(
            "Court-filing scan complete (content + case comparison)."
            if court_filing
            else "Exhibit forensic scan complete."
        ),
        report=report,
    )

    if document_id:
        cases_db.update_document(
            document_id,
            status="complete",
            risk_level=risk_level,
            risk_score=risk_score,
            scan_job_id=job_id,
            last_scanned_at=datetime.now(timezone.utc).isoformat(),
        )
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

    duration_ms = int((time.monotonic() - started) * 1000)
    emit_scan_completed(
        job_id=job_id,
        duration_ms=duration_ms,
        risk_level=risk_level,
        finding_counts=finding_count_summary(findings),
    )
    return report
