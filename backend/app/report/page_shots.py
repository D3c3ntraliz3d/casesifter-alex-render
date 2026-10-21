"""Cropped / highlighted page screenshots for report PDF + HTML export.

Generated while the source file is still on disk, then stored as JPEG data URLs
on each finding's evidence so durable/hosted exports survive source deletion.
Never invents bounding boxes — highlight only when real region coords exist.
"""

from __future__ import annotations

import base64
import io
import logging
import re
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont

from app.models import Finding, Report

logger = logging.getLogger("uvicorn.error")

# Keep export PDFs lean.
_MAX_SHOTS = 10
_MAX_EDGE_PX = 900
_JPEG_QUALITY = 72
_MAX_BYTES = 220_000
_SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2, "low": 3, "info": 9}


def attach_finding_page_shots(
    report: Report,
    source: Path | None,
    *,
    force: bool = False,
) -> Report:
    """Attach evidence.page_shot JPEGs for material findings with page anchors.

    No-op when source is missing or shots already present (unless force=True).
    """
    if source is None or not Path(source).exists():
        return report
    path = Path(source)
    clone = report.model_copy(deep=True)
    candidates = _shot_candidates(clone.findings)
    if not candidates:
        return clone

    page_cache: dict[int, Image.Image] = {}

    def page_image(page_no: int) -> Image.Image | None:
        if page_no in page_cache:
            return page_cache[page_no]
        rendered = _render_one_page(path, page_no)
        if rendered is not None:
            page_cache[page_no] = rendered
        return rendered

    attached = 0
    shots_by_id: dict[str, dict[str, Any]] = {}
    for finding in candidates:
        if attached >= _MAX_SHOTS:
            break
        ev = finding.evidence if isinstance(finding.evidence, dict) else {}
        if not force and isinstance(ev.get("page_shot"), str) and ev["page_shot"].startswith("data:image"):
            continue
        page_no = _finding_page(finding)
        if page_no is None:
            continue
        base = page_image(page_no)
        if base is None:
            continue
        bbox = _extract_bbox(ev, base.size)
        shot, kind = _build_shot(
            base,
            page_no=page_no,
            title=finding.title or "Finding",
            bbox=bbox,
        )
        data_url = _to_jpeg_data_url(shot)
        if not data_url:
            continue
        patch = {
            "page_shot": data_url,
            "page_shot_kind": kind,
            "page_shot_page": page_no,
        }
        shots_by_id[finding.id] = patch
        attached += 1

    def _apply(item: Finding) -> Finding:
        patch = shots_by_id.get(item.id)
        if not patch:
            return item
        evidence = dict(item.evidence or {})
        evidence.update(patch)
        item.evidence = evidence
        return item

    clone.findings = [_apply(f) for f in clone.findings]
    clone.top_findings = [_apply(f) for f in (clone.top_findings or [])]
    if attached:
        logger.info("page_shots attached=%s job=%s", attached, clone.job_id)
    return clone


def _shot_candidates(findings: list[Finding]) -> list[Finding]:
    rows = [
        f
        for f in (findings or [])
        if f.severity != "info" and _finding_page(f) is not None
    ]
    rows.sort(
        key=lambda f: (
            _SEVERITY_ORDER.get(str(f.severity), 5),
            -float(f.confidence or 0),
        )
    )
    return rows[:_MAX_SHOTS]


def _finding_page(finding: Finding) -> int | None:
    if finding.page is not None:
        try:
            n = int(finding.page)
            return n if n >= 1 else None
        except (TypeError, ValueError):
            pass
    ev = finding.evidence if isinstance(finding.evidence, dict) else {}
    for key in ("page", "evidence_page", "page_shot_page"):
        raw = ev.get(key)
        if raw is None or raw == "":
            continue
        try:
            n = int(raw)
            if n >= 1:
                return n
        except (TypeError, ValueError):
            continue
    for ref in ev.get("refs") or []:
        if not isinstance(ref, dict):
            continue
        raw = ref.get("page")
        if raw is None or raw == "":
            continue
        try:
            n = int(raw)
            if n >= 1:
                return n
        except (TypeError, ValueError):
            continue
    return None


def _extract_bbox(evidence: dict[str, Any], size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Return pixel bbox (x0,y0,x1,y1) only from real coords — never invent."""
    w, h = size
    raw = evidence.get("bbox") or evidence.get("region") or evidence.get("highlight_bbox")
    parsed = _parse_bbox(raw, w, h)
    if parsed:
        return parsed
    # Pixel box with origin: [x, y, w, h] or {x,y,width,height}
    box = evidence.get("overlay_box") or evidence.get("box_px")
    if isinstance(box, (list, tuple)) and len(box) >= 4:
        try:
            x, y, bw, bh = (int(box[0]), int(box[1]), int(box[2]), int(box[3]))
            if bw > 0 and bh > 0:
                return _clamp_box(x, y, x + bw, y + bh, w, h)
        except (TypeError, ValueError):
            pass
    if isinstance(box, dict):
        try:
            x = int(box.get("x") or box.get("left") or 0)
            y = int(box.get("y") or box.get("top") or 0)
            bw = int(box.get("w") or box.get("width") or 0)
            bh = int(box.get("h") or box.get("height") or 0)
            if bw > 0 and bh > 0:
                return _clamp_box(x, y, x + bw, y + bh, w, h)
        except (TypeError, ValueError):
            pass
    for ref in evidence.get("refs") or []:
        if isinstance(ref, dict):
            parsed = _parse_bbox(ref.get("bbox") or ref.get("region"), w, h)
            if parsed:
                return parsed
    return None


def _parse_bbox(raw: Any, w: int, h: int) -> tuple[int, int, int, int] | None:
    if raw is None:
        return None
    vals: list[float] = []
    if isinstance(raw, dict):
        for key in ("x0", "y0", "x1", "y1"):
            if key in raw:
                try:
                    vals.append(float(raw[key]))
                except (TypeError, ValueError):
                    return None
        if len(vals) != 4 and all(k in raw for k in ("left", "top", "right", "bottom")):
            try:
                vals = [
                    float(raw["left"]),
                    float(raw["top"]),
                    float(raw["right"]),
                    float(raw["bottom"]),
                ]
            except (TypeError, ValueError):
                return None
    elif isinstance(raw, (list, tuple)) and len(raw) >= 4:
        try:
            vals = [float(raw[0]), float(raw[1]), float(raw[2]), float(raw[3])]
        except (TypeError, ValueError):
            return None
    else:
        return None
    if len(vals) != 4:
        return None
    # Normalized 0–1 coords vs pixel coords.
    if max(vals) <= 1.5:
        x0, y0, x1, y1 = vals[0] * w, vals[1] * h, vals[2] * w, vals[3] * h
    else:
        x0, y0, x1, y1 = vals
    if x1 < x0:
        x0, x1 = x1, x0
    if y1 < y0:
        y0, y1 = y1, y0
    if (x1 - x0) < 4 or (y1 - y0) < 4:
        return None
    return _clamp_box(int(x0), int(y0), int(x1), int(y1), w, h)


def _clamp_box(x0: int, y0: int, x1: int, y1: int, w: int, h: int) -> tuple[int, int, int, int]:
    x0 = max(0, min(w - 1, x0))
    y0 = max(0, min(h - 1, y0))
    x1 = max(x0 + 1, min(w, x1))
    y1 = max(y0 + 1, min(h, y1))
    return x0, y0, x1, y1


def _build_shot(
    page: Image.Image,
    *,
    page_no: int,
    title: str,
    bbox: tuple[int, int, int, int] | None,
) -> tuple[Image.Image, str]:
    rgb = page.convert("RGB")
    w, h = rgb.size
    label = f"p.{page_no}  ·  {_short(title, 64)}"
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        # Tight pad — enough context, no sparse white voids around the highlight.
        bw, bh = x1 - x0, y1 - y0
        pad_x = max(10, min(36, int(bw * 0.12)))
        pad_y = max(10, min(40, int(bh * 0.18)))
        cx0 = max(0, x0 - pad_x)
        cy0 = max(0, y0 - pad_y)
        cx1 = min(w, x1 + pad_x)
        cy1 = min(h, y1 + pad_y)
        # Only expand tiny boxes; never force a tall empty band.
        min_h = min(h, max(96, int(bh * 1.35), int(h * 0.12)))
        if (cy1 - cy0) < min_h:
            mid = (cy0 + cy1) // 2
            cy0 = max(0, mid - min_h // 2)
            cy1 = min(h, cy0 + min_h)
            cy0 = max(0, cy1 - min_h)
        annotated = rgb.copy()
        draw = ImageDraw.Draw(annotated, "RGBA")
        # Readable teal wash + crisp border — hugs recorded coords only.
        draw.rectangle(
            [x0, y0, x1, y1],
            fill=(14, 124, 123, 48),
            outline=(180, 35, 24, 235),
            width=max(2, min(3, max(1, int(min(bw, bh) * 0.04)))),
        )
        crop = annotated.crop((cx0, cy0, cx1, cy1))
        return _with_callout(crop, label), "bbox_highlight"

    # Page-only: trim margins and take a zoomed content band (no fake highlight box).
    mx, my = int(w * 0.04), int(h * 0.03)
    trimmed = rgb.crop((mx, my, w - mx, h - my))
    tw, th = trimmed.size
    # Zoom into the upper-middle content band on tall pages — shorter than before.
    if th > tw * 1.15:
        band = int(th * 0.42)
        top = int(th * 0.06)
        crop = trimmed.crop((0, top, tw, min(th, top + band)))
    else:
        crop = trimmed
    return _with_callout(crop, label), "page_zoom"


def _with_callout(image: Image.Image, label: str) -> Image.Image:
    img = image.convert("RGB")
    w, h = img.size
    # Slim navy label bar — hugs the crop instead of a tall banner.
    bar_h = max(22, min(30, int(h * 0.055)))
    canvas = Image.new("RGB", (w, h + bar_h), (20, 50, 74))
    canvas.paste(img, (0, bar_h))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((8, max(4, (bar_h - 11) // 2)), label, fill=(244, 255, 254), font=font)
    # Teal accent hairline (brand, not gold chrome).
    draw.rectangle([0, bar_h - 2, w, bar_h], fill=(14, 124, 123))
    return _fit_max_edge(canvas, _MAX_EDGE_PX)


def _fit_max_edge(image: Image.Image, max_edge: int) -> Image.Image:
    w, h = image.size
    edge = max(w, h)
    if edge <= max_edge:
        return image
    scale = max_edge / float(edge)
    nw, nh = max(1, int(w * scale)), max(1, int(h * scale))
    return image.resize((nw, nh), Image.Resampling.LANCZOS)


def _to_jpeg_data_url(image: Image.Image) -> str | None:
    quality = _JPEG_QUALITY
    raw: bytes | None = None
    for _ in range(4):
        buf = io.BytesIO()
        image.save(buf, format="JPEG", quality=quality, optimize=True)
        raw = buf.getvalue()
        if len(raw) <= _MAX_BYTES:
            break
        quality = max(40, quality - 10)
        if quality == 40 and len(raw) > _MAX_BYTES:
            # Shrink once more.
            image = _fit_max_edge(image, int(max(image.size) * 0.85))
    if not raw:
        return None
    encoded = base64.b64encode(raw).decode("ascii")
    return f"data:image/jpeg;base64,{encoded}"


def _render_one_page(path: Path, page_no: int) -> Image.Image | None:
    """Render a single 1-based page. Images are treated as page 1."""
    suffix = path.suffix.lower()
    try:
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp", ".webp"}:
            if page_no != 1:
                return None
            from app.ingestion import load_image

            return load_image(path)
        if suffix != ".pdf":
            return None
        import pypdfium2 as pdfium

        doc = pdfium.PdfDocument(str(path))
        try:
            if page_no < 1 or page_no > len(doc):
                return None
            page = doc[page_no - 1]
            try:
                # Slightly sharper than default forensic scale for lawyer review.
                bitmap = page.render(scale=1.35)
                return bitmap.to_pil().convert("RGB")
            finally:
                page.close()
        finally:
            doc.close()
    except Exception as exc:
        logger.warning("page_shot render failed page=%s path=%s err=%s", page_no, path.name, exc)
        return None


def _short(text: str, limit: int) -> str:
    cleaned = re.sub(r"\s+", " ", (text or "").strip())
    if len(cleaned) <= limit:
        return cleaned
    return cleaned[: max(0, limit - 1)].rstrip() + "…"


def decode_data_url_image(data_url: str) -> Image.Image | None:
    """Load a data:image/...;base64,... URL into a PIL image."""
    if not isinstance(data_url, str) or not data_url.startswith("data:image"):
        return None
    try:
        _, encoded = data_url.split(",", 1)
        raw = base64.b64decode(encoded)
        return Image.open(io.BytesIO(raw)).convert("RGB")
    except Exception:
        return None
