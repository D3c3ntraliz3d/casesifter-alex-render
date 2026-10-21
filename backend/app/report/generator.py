from __future__ import annotations

import base64
import io
from datetime import datetime, timezone
from pathlib import Path

from jinja2 import Environment, FileSystemLoader, select_autoescape
from reportlab.lib import colors
from reportlab.lib.pagesizes import letter
from reportlab.lib.styles import ParagraphStyle, getSampleStyleSheet
from reportlab.lib.units import inch
from reportlab.platypus import (
    Image as RLImage,
    ListFlowable,
    ListItem,
    Paragraph,
    SimpleDocTemplate,
    Spacer,
    Table,
    TableStyle,
)

from app import __version__
from app.models import Report
from app.report.scoring import risk_banner
from app.storage import artifact_dir, job_dir

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

CATEGORY_LABEL = {
    "metadata": "Metadata",
    "image": "Image manipulation",
    "text": "Text / OCR",
    "pdf_structure": "PDF structure",
    "page_anomalies": "Page anomalies",
    "contextual": "Contextual logic",
    "case_consistency": "Case consistency",
}


def render_html(report: Report, source: Path | None = None) -> str:
    template = env.get_template("report.html")
    prepared = prepare_report_for_export(report, source=source)
    return template.render(
        report=prepared,
        risk_label=risk_banner(prepared.risk_level, prepared.findings),
        category_label=CATEGORY_LABEL,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        version=__version__,
    )


def prepare_report_for_export(report: Report, source: Path | None = None) -> Report:
    """Inline heatmaps + attach page screenshots when the source is still available."""
    from app.report.page_shots import attach_finding_page_shots

    prepared = attach_finding_page_shots(report, source)
    return _embed_heatmaps(prepared)


def _embed_heatmaps(report: Report) -> Report:
    clone = report.model_copy(deep=True)
    for collection in (clone.findings, clone.top_findings):
        for item in collection:
            heat = (item.evidence or {}).get("heatmap")
            if not heat:
                continue
            if isinstance(heat, str) and heat.startswith("data:image"):
                continue
            name = str(heat).rsplit("/", 1)[-1]
            path = artifact_dir(report.job_id) / name
            if path.exists():
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                item.evidence["heatmap"] = f"data:image/png;base64,{encoded}"
    return clone


def render_pdf_bytes(report: Report, source: Path | None = None) -> bytes:
    """Render PDF into memory (hosted durable export; no disk report required)."""
    buf = io.BytesIO()
    _build_pdf(prepare_report_for_export(report, source=source), buf)
    return buf.getvalue()


def render_pdf(report: Report, source: Path | None = None) -> Path:
    out = job_dir(report.job_id) / "report.pdf"
    _build_pdf(prepare_report_for_export(report, source=source), str(out))
    return out


def _evidence_image_flowable(src: str | None, job_id: str, *, max_h: float = 3.4 * inch):
    """Build a ReportLab image from a data URL or job artifact path."""
    if not src or not isinstance(src, str):
        return None
    try:
        from PIL import Image as PILImage

        from app.report.page_shots import decode_data_url_image

        if src.startswith("data:image"):
            image = decode_data_url_image(src)
            if image is None:
                return None
            # ReportLab's platypus Image wants a path or a file-like with .name.
            buf = io.BytesIO()
            image.save(buf, format="JPEG", quality=72, optimize=True)
            buf.seek(0)
            buf.name = "page_shot.jpg"
            aspect = image.height / max(image.width, 1)
            width = 5.8 * inch
            height = min(max_h, width * aspect)
            return RLImage(buf, width=width, height=height)

        name = src.rsplit("/", 1)[-1]
        path = artifact_dir(job_id) / name
        if not path.exists():
            return None
        with PILImage.open(path) as im:
            aspect = im.height / max(im.width, 1)
        width = 5.8 * inch
        height = min(max_h, width * aspect)
        return RLImage(str(path), width=width, height=height)
    except Exception:
        return None


# Brand tokens shared with the CaseSifter web app / marketing mark.
_NAVY = colors.HexColor("#14324a")
_ACCENT = colors.HexColor("#0e7c7b")
_INK = colors.HexColor("#0f1720")
_MUTED = colors.HexColor("#5b6675")
_LINE = colors.HexColor("#e3e8ef")
_SOFT = colors.HexColor("#f3f6fa")


def _brand_masthead(styles) -> Table:
    """CS seal + CaseSifter wordmark — print-safe solid navy (no decorative gradient)."""
    seal = Table(
        [[Paragraph("<font color='white'><b>CS</b></font>", styles["SealText"])]],
        colWidths=[0.48 * inch],
        rowHeights=[0.48 * inch],
    )
    seal.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), _NAVY),
                ("ALIGN", (0, 0), (-1, -1), "CENTER"),
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (-1, -1), 0),
                ("RIGHTPADDING", (0, 0), (-1, -1), 0),
                ("TOPPADDING", (0, 0), (-1, -1), 0),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 0),
            ]
        )
    )
    wordmark = [
        Paragraph("CaseSifter", styles["BrandName"]),
        Paragraph("Find what matters in the record.", styles["BrandTag"]),
    ]
    mast = Table([[seal, wordmark]], colWidths=[0.58 * inch, 5.9 * inch])
    mast.setStyle(
        TableStyle(
            [
                ("VALIGN", (0, 0), (-1, -1), "MIDDLE"),
                ("LEFTPADDING", (0, 0), (0, 0), 0),
                ("RIGHTPADDING", (0, 0), (0, 0), 8),
                ("LEFTPADDING", (1, 0), (1, 0), 4),
                ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                ("BOX", (0, 0), (-1, -1), 0.6, _LINE),
                ("TOPPADDING", (0, 0), (-1, -1), 10),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
            ]
        )
    )
    return mast


def _finding_accent(severity: str) -> colors.Color:
    if severity in {"critical", "high"}:
        return colors.HexColor("#b42318")
    if severity == "medium":
        return colors.HexColor("#9a6b12")
    return _ACCENT


def _build_pdf(report: Report, destination: str | io.BytesIO) -> None:
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="BrandName",
            fontName="Helvetica-Bold",
            fontSize=16,
            leading=19,
            textColor=_NAVY,
        )
    )
    styles.add(
        ParagraphStyle(
            name="BrandTag",
            fontName="Helvetica",
            fontSize=9,
            leading=11,
            textColor=_MUTED,
        )
    )
    styles.add(
        ParagraphStyle(
            name="SealText",
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=13,
            alignment=1,
            textColor=colors.white,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Kicker",
            fontName="Helvetica-Bold",
            fontSize=8,
            leading=10,
            textColor=_ACCENT,
            spaceBefore=2,
            spaceAfter=2,
        )
    )
    styles.add(
        ParagraphStyle(
            name="CoverTitle",
            fontName="Helvetica-Bold",
            fontSize=15,
            leading=19,
            textColor=_NAVY,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section",
            fontName="Helvetica-Bold",
            fontSize=12,
            leading=15,
            textColor=_NAVY,
            spaceBefore=16,
            spaceAfter=8,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Body",
            fontName="Helvetica",
            fontSize=10,
            leading=13,
            textColor=_INK,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Small",
            fontName="Helvetica",
            fontSize=8,
            leading=11,
            textColor=_MUTED,
        )
    )
    styles.add(
        ParagraphStyle(
            name="FindingTitle",
            fontName="Helvetica-Bold",
            fontSize=11,
            leading=14,
            textColor=_NAVY,
            spaceBefore=0,
            spaceAfter=4,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Quote",
            fontName="Helvetica",
            fontSize=9,
            leading=12,
            textColor=_INK,
            leftIndent=4,
            spaceBefore=2,
            spaceAfter=2,
        )
    )

    risk_bg = {
        "green": colors.HexColor("#e5f6ec"),
        "yellow": colors.HexColor("#fbf0d6"),
        "red": colors.HexColor("#fdeceb"),
        "incomplete": colors.HexColor("#e7eef6"),
    }.get(report.risk_level, colors.HexColor("#e7eef6"))
    risk_color = {
        "green": colors.HexColor("#1f7a4d"),
        "yellow": colors.HexColor("#9a6b12"),
        "red": colors.HexColor("#b42318"),
        "incomplete": _NAVY,
    }.get(report.risk_level, _NAVY)

    story = []
    story.append(_brand_masthead(styles))
    story.append(Spacer(1, 14))
    story.append(Paragraph("AUTHENTICITY REPORT", styles["Kicker"]))
    story.append(Paragraph(report.audit.filename, styles["CoverTitle"]))
    story.append(Paragraph(
        f"Software v{__version__} · SHA-256 {report.audit.sha256}",
        styles["Small"],
    ))
    story.append(Spacer(1, 8))
    badge = Table(
        [[Paragraph(
            f"<b>{risk_banner(report.risk_level, report.findings)}</b>"
            f"&nbsp;&nbsp;·&nbsp;&nbsp;{report.risk_score}/100",
            ParagraphStyle(
                "RiskBadge",
                parent=styles["Body"],
                textColor=risk_color,
                fontName="Helvetica-Bold",
                fontSize=10,
            ),
        )]],
        colWidths=[6.5 * inch],
    )
    badge.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), risk_bg),
                ("LEFTPADDING", (0, 0), (-1, -1), 12),
                ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                ("TOPPADDING", (0, 0), (-1, -1), 9),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 9),
                ("BOX", (0, 0), (-1, -1), 0.5, _LINE),
            ]
        )
    )
    story.append(badge)
    story.append(Spacer(1, 8))
    story.append(Paragraph("Executive summary", styles["Section"]))
    story.append(Paragraph(report.executive_summary, styles["Body"]))

    story.append(Paragraph("Recommended actions", styles["Section"]))
    story.append(
        ListFlowable(
            [ListItem(Paragraph(a, styles["Body"])) for a in report.recommended_actions],
            bulletType="bullet",
        )
    )

    story.append(Paragraph("Detailed findings", styles["Section"]))
    if not report.findings:
        if report.ai_review == "incomplete":
            story.append(
                Paragraph(
                    "Visual review did not complete. This file was not cleared.",
                    styles["Body"],
                )
            )
        else:
            story.append(Paragraph("No automated findings were recorded.", styles["Body"]))
    for item in report.findings:
        if item.severity == "info":
            continue
        cat = CATEGORY_LABEL.get(item.category, item.category)
        page = f"source page {item.page}" if item.page else "page not recorded"
        block: list = [
            Paragraph(item.title, styles["FindingTitle"]),
            Paragraph(
                f"{cat} · {item.severity} · {item.confidence:.0f}% confidence · {page}",
                styles["Small"],
            ),
        ]
        ev = item.evidence or {}
        claim_q = ev.get("claim_quote")
        evidence_q = ev.get("evidence_quote") or ev.get("quoted_evidence")
        if claim_q:
            src = ev.get("claim_source") or ""
            block.append(
                Paragraph(
                    f"<b>Claim:</b> “{claim_q}”" + (f" — {src}" if src else ""),
                    styles["Quote"],
                )
            )
        if evidence_q:
            label = ev.get("evidence_label") or "Evidence"
            src = ev.get("evidence_source") or ""
            block.append(
                Paragraph(
                    f"<b>{label}:</b> “{evidence_q}”" + (f" — {src}" if src else ""),
                    styles["Quote"],
                )
            )
        if item.explanation:
            block.append(Paragraph(item.explanation, styles["Body"]))
        if item.why_suspicious:
            block.append(
                Paragraph(
                    f"<b>Why it matters:</b> {item.why_suspicious}",
                    styles["Small"],
                )
            )
        page_shot = ev.get("page_shot")
        shot_kind = ev.get("page_shot_kind") or ""
        if page_shot:
            kind_note = (
                "Highlighted region (from recorded coordinates)."
                if shot_kind == "bbox_highlight"
                else "Zoomed page crop — coordinates were not recorded for a tighter highlight."
            )
            block.append(Paragraph(f"<b>Exhibit screenshot:</b> {kind_note}", styles["Small"]))
            flow = _evidence_image_flowable(page_shot, report.job_id, max_h=3.4 * inch)
            if flow is not None:
                block.append(Spacer(1, 4))
                block.append(flow)
        heatmap = ev.get("heatmap")
        if heatmap:
            flow = _evidence_image_flowable(heatmap, report.job_id, max_h=3.0 * inch)
            if flow is not None:
                block.append(Paragraph("<b>Forensic overlay</b>", styles["Small"]))
                block.append(flow)

        card = Table([[block]], colWidths=[6.5 * inch])
        card.setStyle(
            TableStyle(
                [
                    ("BACKGROUND", (0, 0), (-1, -1), colors.white),
                    ("BOX", (0, 0), (-1, -1), 0.6, _LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 12),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 12),
                    ("TOPPADDING", (0, 0), (-1, -1), 10),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 10),
                    ("LINEBEFORE", (0, 0), (0, 0), 3.2, _finding_accent(str(item.severity))),
                ]
            )
        )
        story.append(card)
        story.append(Spacer(1, 8))

    if report.timeline:
        story.append(Paragraph("Timeline", styles["Section"]))
        for event in report.timeline:
            mark = " ⚠" if event.anomaly else ""
            story.append(
                Paragraph(
                    f"<b>{event.label}</b>{mark} — {event.timestamp or 'unknown'} ({event.source})",
                    styles["Body"],
                )
            )
            if event.note:
                story.append(Paragraph(event.note, styles["Small"]))

    if report.page_dimensions:
        story.append(Paragraph("Page dimensions", styles["Section"]))
        header = ["Page", "W (pt)", "H (pt)", "W (px)", "H (px)", "DPI", "Orient."]
        rows = [header]
        for dim in report.page_dimensions:
            rows.append(
                [
                    str(dim.page),
                    _fmt(dim.width_pts),
                    _fmt(dim.height_pts),
                    _fmt(dim.width_px),
                    _fmt(dim.height_px),
                    _fmt(dim.dpi),
                    dim.orientation or "",
                ]
            )
        table = Table(
            rows,
            colWidths=[0.7 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.7 * inch, 0.9 * inch],
        )
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, 0), "Helvetica-Bold"),
                    ("FONTNAME", (0, 1), (-1, -1), "Helvetica"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("TEXTCOLOR", (0, 0), (-1, 0), _MUTED),
                    ("BACKGROUND", (0, 0), (-1, 0), _SOFT),
                    ("LINEBELOW", (0, 0), (-1, -2), 0.4, _LINE),
                    ("LEFTPADDING", (0, 0), (-1, -1), 5),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 5),
                    ("TOPPADDING", (0, 0), (-1, -1), 5),
                    ("BOTTOMPADDING", (0, 0), (-1, -1), 5),
                    ("BOX", (0, 0), (-1, -1), 0.5, _LINE),
                ]
            )
        )
        story.append(table)

    story.append(Paragraph("Audit trail", styles["Section"]))
    audit_lines = [
        f"Scanned at: {report.audit.scanned_at.isoformat()}",
        f"Submitted by: {report.audit.submitted_by}",
        f"Analysis version: {report.audit.analysis_version}",
        f"Modules: {', '.join(report.audit.modules_run)}",
        f"File type: {report.audit.file_type} ({report.audit.file_size_bytes} bytes)",
        f"SHA-256: {report.audit.sha256}",
    ]
    for line in audit_lines:
        story.append(Paragraph(line, styles["Small"]))
    story.append(Spacer(1, 12))
    story.append(Paragraph(report.disclaimer, styles["Small"]))

    doc = SimpleDocTemplate(
        destination,
        pagesize=letter,
        title=f"CaseSifter — {report.audit.filename}",
        leftMargin=0.7 * inch,
        rightMargin=0.7 * inch,
        topMargin=0.65 * inch,
        bottomMargin=0.65 * inch,
    )
    doc.build(story)


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)
