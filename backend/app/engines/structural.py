from __future__ import annotations

from collections import Counter
from pathlib import Path
from typing import Any

from app.engines import finding, timed_module
from app.ingestion import load_image, open_pdf_reader, render_pdf_pages
from app.models import Finding, ModuleResult, PageDimension


def analyze_structural(path: Path) -> ModuleResult:
    return timed_module("structural", lambda: _run(path))


def _run(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        return _pdf_structure(path)
    if suffix in {".jpg", ".jpeg", ".png", ".gif"}:
        return _image_structure(path)
    if suffix in {".docx", ".xlsx"}:
        return [], {"note": "Page-level geometry is reported for PDFs and images."}
    return [], {}


def _pdf_structure(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    reader = open_pdf_reader(path)
    dims: list[PageDimension] = []
    rotations = []
    for i, page in enumerate(reader.pages, start=1):
        box = page.mediabox
        width = float(box.width)
        height = float(box.height)
        rotation = int(page.get("/Rotate") or 0)
        rotations.append(rotation)
        dims.append(
            PageDimension(
                page=i,
                width_pts=round(width, 2),
                height_pts=round(height, 2),
                rotation=rotation,
                orientation="landscape" if width > height else "portrait",
            )
        )

    widths = [d.width_pts or 0 for d in dims]
    heights = [d.height_pts or 0 for d in dims]
    if widths and (max(widths) - min(widths) > 18 or max(heights) - min(heights) > 18):
        findings.append(
            finding(
                fid="st-page-size",
                category="page_anomalies",
                severity="medium",
                title="Page size is not consistent throughout the PDF",
                explanation=(
                    f"Width ranges from {min(widths):.1f} to {max(widths):.1f} pt; "
                    f"height from {min(heights):.1f} to {max(heights):.1f} pt. "
                    "Mixed letter/legal/A4 pages, or a page inserted from another file, produce this pattern."
                ),
                why_suspicious="Exhibits assembled from different sources often keep each source’s paper size.",
                confidence=73,
                evidence={"min_width": min(widths), "max_width": max(widths)},
                recommended_action="Look at the page-dimension table and visually compare the odd-sized pages.",
            )
        )

    rot_counts = Counter(rotations)
    if len([r for r in rot_counts if r % 180 != 0]) and len(rot_counts) > 1:
        findings.append(
            finding(
                fid="st-rotation",
                category="page_anomalies",
                severity="low",
                title="Mixed page rotations",
                explanation=f"Rotation values present: {dict(rot_counts)}.",
                why_suspicious="A single scanned packet is usually aligned; mixed rotation can mean inserted pages.",
                confidence=50,
                evidence={"rotations": dict(rot_counts)},
            )
        )

    # Render a sample to compare visual resolution between pages.
    try:
        rendered = render_pdf_pages(path, max_pages=min(12, len(reader.pages)))
        pixel_heights = [im.height for im in rendered]
        if pixel_heights and max(pixel_heights) > min(pixel_heights) * 1.35:
            findings.append(
                finding(
                    fid="st-resolution-mix",
                    category="page_anomalies",
                    severity="medium",
                    title="Rendered page resolution differs substantially",
                    explanation=(
                        f"Among the first {len(pixel_heights)} pages, pixel height ranges from "
                        f"{min(pixel_heights)} to {max(pixel_heights)}. Different scanners or a mix of "
                        "born-digital and photographed pages often cause this."
                    ),
                    why_suspicious="A packet from one device usually has even scan quality.",
                    confidence=66,
                    evidence={"min_px": min(pixel_heights), "max_px": max(pixel_heights)},
                )
            )
        for dim, im in zip(dims, rendered):
            dim.width_px = im.width
            dim.height_px = im.height
            if dim.width_pts:
                dim.dpi = round(im.width / (dim.width_pts / 72.0), 1)
    except Exception:
        pass

    labels = _page_labels(reader)
    if labels:
        extras_labels = labels
        dupes = [n for n, c in Counter(labels).items() if c > 1 and n is not None]
        if dupes:
            findings.append(
                finding(
                    fid="st-dup-labels",
                    category="page_anomalies",
                    severity="medium",
                    title="Duplicate page labels",
                    explanation=f"Repeated labels: {dupes[:10]}.",
                    why_suspicious="Duplicate numbering can hide inserted or replaced pages.",
                    confidence=60,
                )
            )
    else:
        extras_labels = list(range(1, len(dims) + 1))

    extras = {
        "page_dimensions": [d.model_dump() for d in dims],
        "page_labels": extras_labels,
        "dimension_models": dims,
    }
    return findings, extras


def _page_labels(reader) -> list[str | int] | None:
    try:
        root = reader.trailer.get("/Root")
        if root is None:
            return None
        root = root.get_object() if hasattr(root, "get_object") else root
        nums = root.get("/PageLabels")
        return None if nums is None else ["(custom page labels present)"]
    except Exception:
        return None


def _image_structure(path: Path) -> tuple[list[Finding], dict[str, Any]]:
    image = load_image(path)
    dpi = None
    from PIL import Image as PILImage

    with PILImage.open(path) as original:
        raw_dpi = original.info.get("dpi")
    if raw_dpi:
        try:
            dpi = float(raw_dpi[0] if isinstance(raw_dpi, tuple) else raw_dpi)
        except (TypeError, ValueError):
            dpi = None
    dim = PageDimension(
        page=1,
        width_px=image.width,
        height_px=image.height,
        dpi=dpi,
        orientation="landscape" if image.width > image.height else "portrait",
    )
    extras = {"page_dimensions": [dim.model_dump()], "dimension_models": [dim]}
    return [], extras
