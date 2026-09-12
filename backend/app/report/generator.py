from __future__ import annotations

import base64
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
from app.storage import artifact_dir, job_dir

TEMPLATE_DIR = Path(__file__).resolve().parent / "templates"
env = Environment(
    loader=FileSystemLoader(str(TEMPLATE_DIR)),
    autoescape=select_autoescape(["html"]),
)

RISK_LABEL = {"green": "Low risk", "yellow": "Moderate risk", "red": "High risk"}
CATEGORY_LABEL = {
    "metadata": "Metadata",
    "image": "Image manipulation",
    "text": "Text / OCR",
    "pdf_structure": "PDF structure",
    "page_anomalies": "Page anomalies",
    "contextual": "Contextual logic",
    "case_consistency": "Case consistency",
}


def render_html(report: Report) -> str:
    template = env.get_template("report.html")
    return template.render(
        report=_embed_heatmaps(report),
        risk_label=RISK_LABEL.get(report.risk_level, report.risk_level),
        category_label=CATEGORY_LABEL,
        generated=datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC"),
        version=__version__,
    )


def _embed_heatmaps(report: Report) -> Report:
    clone = report.model_copy(deep=True)
    for collection in (clone.findings, clone.top_findings):
        for item in collection:
            heat = (item.evidence or {}).get("heatmap")
            if not heat:
                continue
            name = str(heat).rsplit("/", 1)[-1]
            path = artifact_dir(report.job_id) / name
            if path.exists():
                encoded = base64.b64encode(path.read_bytes()).decode("ascii")
                item.evidence["heatmap"] = f"data:image/png;base64,{encoded}"
    return clone


def render_pdf(report: Report) -> Path:
    out = job_dir(report.job_id) / "report.pdf"
    styles = getSampleStyleSheet()
    styles.add(
        ParagraphStyle(
            name="CoverTitle",
            fontName="Times-Bold",
            fontSize=18,
            leading=22,
            textColor=colors.HexColor("#1a365d"),
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Section",
            fontName="Times-Bold",
            fontSize=13,
            leading=16,
            textColor=colors.HexColor("#1a365d"),
            spaceBefore=14,
            spaceAfter=6,
        )
    )
    styles.add(
        ParagraphStyle(
            name="Body",
            fontName="Times-Roman",
            fontSize=10,
            leading=13,
            textColor=colors.HexColor("#1a202c"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="Small",
            fontName="Times-Roman",
            fontSize=8,
            leading=11,
            textColor=colors.HexColor("#4a5568"),
        )
    )
    styles.add(
        ParagraphStyle(
            name="FindingTitle",
            fontName="Times-Bold",
            fontSize=11,
            leading=14,
            textColor=colors.HexColor("#1a365d"),
            spaceBefore=8,
        )
    )

    risk_color = {
        "green": colors.HexColor("#276749"),
        "yellow": colors.HexColor("#b7791f"),
        "red": colors.HexColor("#c53030"),
    }[report.risk_level]

    story = []
    story.append(Paragraph("LawSifter — Authenticity Report", styles["CoverTitle"]))
    story.append(Paragraph(report.audit.filename, styles["Body"]))
    story.append(Spacer(1, 8))
    badge = Table(
        [[Paragraph(f"<b>{RISK_LABEL[report.risk_level]}</b>  (score {report.risk_score}/100)", styles["Body"])]],
        colWidths=[6.5 * inch],
    )
    badge.setStyle(
        TableStyle(
            [
                ("BACKGROUND", (0, 0), (-1, -1), colors.HexColor("#edf2f7")),
                ("TEXTCOLOR", (0, 0), (-1, -1), risk_color),
                ("LEFTPADDING", (0, 0), (-1, -1), 10),
                ("TOPPADDING", (0, 0), (-1, -1), 8),
                ("BOTTOMPADDING", (0, 0), (-1, -1), 8),
            ]
        )
    )
    story.append(badge)
    story.append(Spacer(1, 10))
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
        story.append(Paragraph("No automated findings were recorded.", styles["Body"]))
    for item in report.findings:
        if item.severity == "info":
            continue
        cat = CATEGORY_LABEL.get(item.category, item.category)
        page = f" — page {item.page}" if item.page else ""
        story.append(
            Paragraph(
                f"{item.title}{page}  ({cat}, {item.severity}, {item.confidence:.0f}% confidence)",
                styles["FindingTitle"],
            )
        )
        story.append(Paragraph(item.explanation, styles["Body"]))
        story.append(Paragraph(f"<i>Why this is flagged:</i> {item.why_suspicious}", styles["Small"]))
        heatmap = (item.evidence or {}).get("heatmap")
        if heatmap:
            name = str(heatmap).rsplit("/", 1)[-1]
            img_path = artifact_dir(report.job_id) / name
            if img_path.exists():
                story.append(Spacer(1, 4))
                try:
                    from PIL import Image as PILImage

                    with PILImage.open(img_path) as im:
                        aspect = im.height / max(im.width, 1)
                    width = 5.8 * inch
                    height = min(4.2 * inch, width * aspect)
                    story.append(RLImage(str(img_path), width=width, height=height))
                except Exception:
                    pass

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
        table = Table(rows, colWidths=[0.7 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.9 * inch, 0.7 * inch, 0.9 * inch])
        table.setStyle(
            TableStyle(
                [
                    ("FONTNAME", (0, 0), (-1, 0), "Times-Bold"),
                    ("FONTSIZE", (0, 0), (-1, -1), 8),
                    ("BACKGROUND", (0, 0), (-1, 0), colors.HexColor("#edf2f7")),
                    ("GRID", (0, 0), (-1, -1), 0.3, colors.HexColor("#cbd5e0")),
                    ("LEFTPADDING", (0, 0), (-1, -1), 4),
                    ("RIGHTPADDING", (0, 0), (-1, -1), 4),
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
        str(out),
        pagesize=letter,
        title=f"Exhibit scan — {report.audit.filename}",
        leftMargin=0.75 * inch,
        rightMargin=0.75 * inch,
        topMargin=0.7 * inch,
        bottomMargin=0.7 * inch,
    )
    doc.build(story)
    return out


def _fmt(value) -> str:
    if value is None:
        return "—"
    if isinstance(value, float):
        return f"{value:.1f}"
    return str(value)
