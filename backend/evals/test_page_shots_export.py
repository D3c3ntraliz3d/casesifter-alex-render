"""Page screenshots for export PDF: real crops, bbox highlight only when coords exist."""

from __future__ import annotations

import io
import uuid
from datetime import datetime, timezone
from pathlib import Path

from PIL import Image, ImageDraw


def _tiny_pdf(path: Path, pages: int = 2) -> None:
    """Minimal multi-page PDF via reportlab."""
    from reportlab.pdfgen import canvas

    c = canvas.Canvas(str(path))
    for i in range(pages):
        c.setFont("Helvetica", 14)
        c.drawString(72, 720, f"Exhibit page {i + 1}")
        c.drawString(72, 680, "Claim: account balance is $1,200.00")
        c.drawString(72, 640, "Evidence line with disputed amount")
        c.showPage()
    c.save()


def _report_with_finding(job_id: str, *, page: int, bbox=None):
    from app.models import AuditTrail, Finding, Report

    evidence = {"claim_quote": "balance is $1,200.00", "evidence_quote": "disputed amount"}
    if bbox is not None:
        evidence["bbox"] = bbox
    return Report(
        job_id=job_id,
        risk_level="yellow",
        risk_score=40,
        ai_review="complete",
        executive_summary="Fixture with one material finding.",
        recommended_actions=["Review the highlighted page."],
        findings=[
            Finding(
                id="f1",
                category="contextual",
                severity="high",
                confidence=88,
                title="Balance figure does not match the cited statement line",
                explanation="The cited balance conflicts with the statement body.",
                why_suspicious="Same-account figure runs against the claim.",
                page=page,
                evidence=evidence,
            )
        ],
        top_findings=[],
        timeline=[],
        page_dimensions=[],
        fonts=[],
        comparison={},
        modules=[],
        audit=AuditTrail(
            scanned_at=datetime.now(timezone.utc),
            analysis_version="test",
            modules_run=["smoke"],
            submitted_by="page-shots",
            filename="fixture.pdf",
            file_size_bytes=100,
            file_type="application/pdf",
            sha256="c" * 64,
        ),
        artifact_urls=[],
    )


def test_attach_page_zoom_without_inventing_bbox(tmp_path):
    from app.report.page_shots import attach_finding_page_shots

    pdf = tmp_path / "ex.pdf"
    _tiny_pdf(pdf, pages=2)
    job_id = uuid.uuid4().hex
    report = _report_with_finding(job_id, page=2)
    out = attach_finding_page_shots(report, pdf)
    ev = out.findings[0].evidence
    assert ev.get("page_shot", "").startswith("data:image/jpeg;base64,")
    assert ev.get("page_shot_kind") == "page_zoom"
    assert ev.get("page_shot_page") == 2
    # No bbox was supplied — must not claim a highlight.
    assert ev.get("page_shot_kind") != "bbox_highlight"


def test_attach_bbox_highlight_when_coords_exist(tmp_path):
    from app.report.page_shots import attach_finding_page_shots, decode_data_url_image

    pdf = tmp_path / "ex.pdf"
    _tiny_pdf(pdf, pages=1)
    job_id = uuid.uuid4().hex
    # Normalized bbox around mid-page text band.
    report = _report_with_finding(job_id, page=1, bbox=[0.08, 0.12, 0.75, 0.28])
    out = attach_finding_page_shots(report, pdf)
    ev = out.findings[0].evidence
    assert ev.get("page_shot_kind") == "bbox_highlight"
    img = decode_data_url_image(ev["page_shot"])
    assert img is not None
    assert max(img.size) <= 900


def test_export_pdf_embeds_page_shot(tmp_path):
    from app.report.generator import render_pdf_bytes
    from app.report.page_shots import attach_finding_page_shots

    pdf = tmp_path / "ex.pdf"
    _tiny_pdf(pdf, pages=1)
    job_id = uuid.uuid4().hex
    report = attach_finding_page_shots(_report_with_finding(job_id, page=1), pdf)
    blob = render_pdf_bytes(report)
    assert blob[:4] == b"%PDF"
    # Page shot JPEG bytes should appear inside the PDF stream.
    assert b"/Subtype /Image" in blob or b"/Subtype/Image" in blob
    assert len(blob) > 2_000


def test_no_source_leaves_findings_unchanged(tmp_path):
    from app.report.page_shots import attach_finding_page_shots

    job_id = uuid.uuid4().hex
    report = _report_with_finding(job_id, page=1)
    out = attach_finding_page_shots(report, tmp_path / "missing.pdf")
    assert "page_shot" not in (out.findings[0].evidence or {})
