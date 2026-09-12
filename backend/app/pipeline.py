from __future__ import annotations

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
from app.config import settings
from app.engines.contextual_logic import analyze_contextual
from app.engines.image_forensics import analyze_image_forensics
from app.engines.metadata import analyze_metadata
from app.engines.ocr_text import analyze_ocr_text
from app.engines.pdf_integrity import analyze_pdf_integrity
from app.engines.structural import analyze_structural
from app.ingestion import sha256_file, sniff_type
from app.models import AuditTrail, Finding, FontUsage, PageDimension, Report, TimelineEvent
from app.report import executive_summary, pick_top_findings, recommended_actions, render_html, render_pdf, score_risk
from app.storage import job_dir, update_job
from app.telemetry import emit_scan_outcomes


def _source_for_contextual(source: Path) -> Path | None:
    """Hand the original file to tier-3 analysis, and only then.

    Returns None unless multimodal analysis is explicitly enabled, so a default install
    uploads nothing.
    """
    if not (settings.contextual_multimodal and settings.gemini_api_key):
        return None
    return source


def run_pipeline(
    job_id: str,
    source: Path,
    submitted_by: str = "court-staff",
    *,
    case_id: str | None = None,
    document_id: str | None = None,
    display_name: str | None = None,
    doc_kind: str | None = None,
) -> Report:
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

    update_job(job_id, stage="pdf_integrity", percent=30, message="Checking PDF structure and hidden content…")
    pdf = analyze_pdf_integrity(source)

    update_job(job_id, stage="structural", percent=45, message="Comparing page sizes and orientation…")
    structural = analyze_structural(source)

    update_job(job_id, stage="image_forensics", percent=60, message="Running image manipulation detection…")
    images = analyze_image_forensics(source, job_id)

    update_job(job_id, stage="ocr_text", percent=78, message="Extracting text and checking fonts…")
    ocr = analyze_ocr_text(source, metadata.extras)
    full_text = ocr.extras.pop("full_text", "") or ""

    update_job(job_id, stage="contextual", percent=88, message="Checking entities and timeline logic…")
    doc_meta = (metadata.extras.get("metadata") or {}).get("document") or {}
    contextual = analyze_contextual(
        full_text,
        doc_meta,
        source_file=_source_for_contextual(source),
    )

    update_job(job_id, stage="generating_report", percent=94, message="Compiling the authenticity report…")

    modules = [metadata, pdf, structural, images, ocr, contextual]
    findings: list[Finding] = []
    for module in modules:
        findings.extend(module.findings)

    entities = (contextual.extras or {}).get("entities") or {}
    amounts = extract_amounts(full_text)
    fingerprint = text_fingerprint(full_text)
    hashes = excerpt_hashes(full_text)

    if case_id and document_id:
        update_job(
            job_id,
            stage="case_consistency",
            percent=92,
            message="Comparing against prior documents in this case…",
        )
        cases_db.save_scan_snapshot(
            document_id=document_id,
            case_id=case_id,
            entities=entities,
            findings_summary=findings_summary(findings),
            text_fingerprint=fingerprint,
            excerpt_hashes=hashes,
            amounts=amounts,
        )
        priors = cases_db.list_prior_snapshots(case_id, exclude_document_id=document_id)
        name = display_name or source.name
        consistency = compare_to_case(
            new_document_id=document_id,
            new_display_name=name,
            new_entities=entities,
            new_amounts=amounts,
            new_fingerprint=fingerprint,
            prior_snapshots=priors,
        )
        findings.extend(consistency)
        contextual.extras["case_consistency"] = {
            "prior_documents": len(priors),
            "alerts": len(consistency),
        }

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
        # HTML export still succeeds if PDF layout fails on an odd image.
        pass

    update_job(
        job_id,
        status="complete",
        stage="complete",
        percent=100,
        message="Analysis complete.",
        report=report,
    )

    if document_id:
        cases_db.update_document(
            document_id,
            status="complete",
            risk_level=risk_level,
            risk_score=risk_score,
            scan_job_id=job_id,
        )
        # Refresh snapshot findings to include consistency alerts.
        cases_db.save_scan_snapshot(
            document_id=document_id,
            case_id=case_id or "",
            entities=entities,
            findings_summary=findings_summary(findings),
            text_fingerprint=fingerprint,
            excerpt_hashes=hashes,
            amounts=amounts,
        )
        emit_scan_outcomes(doc_kind=doc_kind, findings=findings, risk_level=risk_level)

    return report
