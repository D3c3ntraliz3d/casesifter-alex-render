"""Cropped / highlighted page screenshots for report PDF + HTML export.

Generated while the source file is still on disk, then stored as JPEG data URLs
on each finding's evidence so durable/hosted exports survive source deletion.

When real bbox coords exist, they are used. Otherwise we locate claim/evidence
quotes on the PDF page (pdfium text search) and crop around that region.
Annotated crops bake in a red circle plus a short note — not a client overlay.
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
_RENDER_SCALE = 1.35


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
        quote_hit = False
        if bbox is None:
            bbox = _bbox_from_quotes(path, page_no, finding, base.size)
            quote_hit = bbox is not None
        note = _annotation_note(finding)
        shot, kind = _build_shot(
            base,
            page_no=page_no,
            title=finding.title or "Finding",
            bbox=bbox,
            note=note,
            force_circle=quote_hit or bbox is not None,
        )
        data_url = _to_jpeg_data_url(shot)
        if not data_url:
            continue
        patch = {
            "page_shot": data_url,
            "page_shot_kind": kind,
            "page_shot_page": page_no,
            "page_shot_note": note,
        }
        if bbox is not None:
            w, h = base.size
            patch["page_shot_bbox"] = [
                round(bbox[0] / w, 4),
                round(bbox[1] / h, 4),
                round(bbox[2] / w, 4),
                round(bbox[3] / h, 4),
            ]
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


def _annotation_note(finding: Finding) -> str:
    """Short durable note baked into the annotated crop."""
    ev = finding.evidence if isinstance(finding.evidence, dict) else {}
    for key in (
        "attorney_why",
        "materiality_reason",
        "page_shot_note",
        "why",
    ):
        raw = ev.get(key)
        if isinstance(raw, str) and raw.strip():
            return _short(raw, 140)
    if finding.why_suspicious and str(finding.why_suspicious).strip():
        return _short(str(finding.why_suspicious), 140)
    if finding.explanation and str(finding.explanation).strip():
        return _short(str(finding.explanation), 140)
    if finding.title and str(finding.title).strip():
        return _short(str(finding.title), 140)
    return "Evidence region"


def _quote_needles(finding: Finding) -> list[str]:
    ev = finding.evidence if isinstance(finding.evidence, dict) else {}
    needles: list[str] = []
    for key in (
        "evidence_quote",
        "quoted_evidence",
        "claim_quote",
        "quote",
    ):
        raw = ev.get(key)
        if isinstance(raw, str) and raw.strip():
            needles.append(raw.strip())
    for ref in ev.get("refs") or []:
        if isinstance(ref, dict) and isinstance(ref.get("quote"), str) and ref["quote"].strip():
            needles.append(ref["quote"].strip())
    # Prefer longer, more specific needles first.
    uniq: list[str] = []
    seen: set[str] = set()
    for n in sorted(needles, key=len, reverse=True):
        key = re.sub(r"\s+", " ", n.lower())
        if key in seen or len(key) < 6:
            continue
        seen.add(key)
        uniq.append(n)
        if len(uniq) >= 4:
            break
    return uniq


def _bbox_from_quotes(
    path: Path,
    page_no: int,
    finding: Finding,
    size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    needles = _quote_needles(finding)
    if not needles:
        return None
    return _search_page_for_quotes(path, page_no, needles, size)


def _search_page_for_quotes(
    path: Path,
    page_no: int,
    needles: list[str],
    size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    """Locate quote text on a PDF page; return pixel bbox in rendered image space."""
    if path.suffix.lower() != ".pdf":
        return None
    try:
        import pypdfium2 as pdfium
    except Exception:
        return None
    try:
        doc = pdfium.PdfDocument(str(path))
    except Exception:
        return None
    try:
        if page_no < 1 or page_no > len(doc):
            return None
        page = doc[page_no - 1]
        try:
            textpage = page.get_textpage()
            try:
                page_w = float(page.get_width())
                page_h = float(page.get_height())
                scale_x = size[0] / page_w if page_w else _RENDER_SCALE
                scale_y = size[1] / page_h if page_h else _RENDER_SCALE
                for needle in needles:
                    box = _search_one_needle(textpage, needle, page_h, scale_x, scale_y, size)
                    if box is not None:
                        return box
                    # Try a shortened core when the full quote is OCR-noisy.
                    core = _quote_core(needle)
                    if core and core != needle:
                        box = _search_one_needle(textpage, core, page_h, scale_x, scale_y, size)
                        if box is not None:
                            return box
            finally:
                textpage.close()
        finally:
            page.close()
    except Exception as exc:
        logger.debug("quote bbox search failed page=%s err=%s", page_no, exc)
        return None
    finally:
        doc.close()
    return None


def _quote_core(needle: str) -> str | None:
    cleaned = re.sub(r"\s+", " ", (needle or "").strip())
    if len(cleaned) < 18:
        return None
    # Middle slice often survives OCR noise better than edges.
    mid = cleaned[len(cleaned) // 5 : -len(cleaned) // 5].strip()
    if len(mid) >= 12:
        return mid
    return cleaned[:48]


def _search_one_needle(
    textpage: Any,
    needle: str,
    page_h: float,
    scale_x: float,
    scale_y: float,
    size: tuple[int, int],
) -> tuple[int, int, int, int] | None:
    cleaned = re.sub(r"\s+", " ", (needle or "").strip())
    if len(cleaned) < 4:
        return None
    try:
        searcher = textpage.search(cleaned, match_case=False, match_whole_word=False)
    except Exception:
        return None
    try:
        hit = searcher.get_next()
        if not hit:
            return None
        # pypdfium2 returns (char_index, length)
        if isinstance(hit, tuple) and len(hit) >= 2:
            start, length = int(hit[0]), int(hit[1])
        else:
            start, length = int(hit), max(1, len(cleaned))
        if length <= 0:
            length = max(1, len(cleaned))
        xs: list[float] = []
        ys0: list[float] = []
        ys1: list[float] = []
        for i in range(start, start + length):
            try:
                box = textpage.get_charbox(i)
            except Exception:
                continue
            if not box or len(box) < 4:
                continue
            left, bottom, right, top = box[0], box[1], box[2], box[3]
            xs.extend([left, right])
            ys0.append(bottom)
            ys1.append(top)
        if not xs:
            return None
        # PDF coords: origin bottom-left → image coords origin top-left.
        x0 = min(xs) * scale_x
        x1 = max(xs) * scale_x
        y_top_pdf = max(ys1)
        y_bot_pdf = min(ys0)
        y0 = (page_h - y_top_pdf) * scale_y
        y1 = (page_h - y_bot_pdf) * scale_y
        w, h = size
        return _clamp_box(int(x0), int(y0), int(x1), int(y1), w, h)
    finally:
        try:
            searcher.close()
        except Exception:
            pass


def _extract_bbox(evidence: dict[str, Any], size: tuple[int, int]) -> tuple[int, int, int, int] | None:
    """Return pixel bbox (x0,y0,x1,y1) only from real coords — never invent."""
    w, h = size
    raw = evidence.get("bbox") or evidence.get("region") or evidence.get("highlight_bbox")
    parsed = _parse_bbox(raw, w, h)
    if parsed:
        return parsed
    # Pixel box with origin: [x, y, w, h] or {x,y,width,height}
    # overlay_box_px historically stored [w,h] only — ignore length < 4 (no invent).
    box = (
        evidence.get("overlay_box")
        or evidence.get("box_px")
        or evidence.get("overlay_box_px")
    )
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
    note: str | None = None,
    force_circle: bool = False,
) -> tuple[Image.Image, str]:
    rgb = page.convert("RGB")
    w, h = rgb.size
    label = f"p.{page_no}  ·  {_short(title, 64)}"
    if bbox is not None:
        x0, y0, x1, y1 = bbox
        # Tight pad — enough context, no sparse white voids around the highlight.
        bw, bh = x1 - x0, y1 - y0
        pad_x = max(16, min(48, int(bw * 0.35)))
        pad_y = max(16, min(56, int(bh * 0.55)))
        cx0 = max(0, x0 - pad_x)
        cy0 = max(0, y0 - pad_y)
        cx1 = min(w, x1 + pad_x)
        cy1 = min(h, y1 + pad_y)
        # Only expand tiny boxes; never force a tall empty band.
        min_h = min(h, max(110, int(bh * 1.8), int(h * 0.14)))
        min_w = min(w, max(160, int(bw * 1.5), int(w * 0.28)))
        if (cy1 - cy0) < min_h:
            mid = (cy0 + cy1) // 2
            cy0 = max(0, mid - min_h // 2)
            cy1 = min(h, cy0 + min_h)
            cy0 = max(0, cy1 - min_h)
        if (cx1 - cx0) < min_w:
            mid = (cx0 + cx1) // 2
            cx0 = max(0, mid - min_w // 2)
            cx1 = min(w, cx0 + min_w)
            cx0 = max(0, cx1 - min_w)
        annotated = rgb.copy()
        _draw_red_circle(annotated, (x0, y0, x1, y1))
        crop = annotated.crop((cx0, cy0, cx1, cy1))
        kind = "annotated_region" if force_circle or note else "bbox_highlight"
        return _with_callout(crop, label, note=note), kind

    # Page-only fallback: trim margins and take a zoomed content band.
    mx, my = int(w * 0.04), int(h * 0.03)
    trimmed = rgb.crop((mx, my, w - mx, h - my))
    tw, th = trimmed.size
    if th > tw * 1.15:
        band = int(th * 0.42)
        top = int(th * 0.06)
        crop = trimmed.crop((0, top, tw, min(th, top + band)))
    else:
        crop = trimmed
    return _with_callout(crop, label, note=note), "page_zoom"


def _draw_red_circle(image: Image.Image, bbox: tuple[int, int, int, int]) -> None:
    """Bake a clear red ellipse around the evidence region (durable, not overlay)."""
    x0, y0, x1, y1 = bbox
    draw = ImageDraw.Draw(image, "RGBA")
    bw, bh = max(1, x1 - x0), max(1, y1 - y0)
    # Expand slightly so the circle rings the quote rather than covering it.
    pad = max(6, int(min(bw, bh) * 0.25), int(max(bw, bh) * 0.08))
    ex0 = max(0, x0 - pad)
    ey0 = max(0, y0 - pad)
    ex1 = min(image.size[0] - 1, x1 + pad)
    ey1 = min(image.size[1] - 1, y1 + pad)
    # Prefer a near-circle when the box is a thin text line.
    if (ex1 - ex0) > 3 * (ey1 - ey0):
        mid_y = (ey0 + ey1) // 2
        radius = max((ey1 - ey0) // 2 + pad, int((ex1 - ex0) * 0.08))
        ey0 = max(0, mid_y - radius)
        ey1 = min(image.size[1] - 1, mid_y + radius)
    width = max(3, min(6, int(min(image.size) * 0.006)))
    # Soft wash inside, then crisp red ring.
    draw.ellipse([ex0, ey0, ex1, ey1], fill=(220, 38, 38, 36), outline=(200, 16, 16, 240), width=width)
    # Second outer ring for visibility on dark scans.
    draw.ellipse(
        [ex0 - 2, ey0 - 2, ex1 + 2, ey1 + 2],
        outline=(255, 255, 255, 180),
        width=max(1, width - 2),
    )


def _with_callout(image: Image.Image, label: str, note: str | None = None) -> Image.Image:
    img = image.convert("RGB")
    w, h = img.size
    # Slim navy label bar — hugs the crop instead of a tall banner.
    bar_h = max(22, min(30, int(h * 0.055)))
    note_clean = _short(note or "", 160) if note else ""
    note_h = 0
    note_lines: list[str] = []
    if note_clean:
        note_lines = _wrap_note(note_clean, max_chars=max(28, w // 7))
        note_h = 10 + len(note_lines) * 14
    canvas = Image.new("RGB", (w, h + bar_h + note_h), (20, 50, 74))
    canvas.paste(img, (0, bar_h))
    draw = ImageDraw.Draw(canvas)
    font = ImageFont.load_default()
    draw.text((8, max(4, (bar_h - 11) // 2)), label, fill=(244, 255, 254), font=font)
    # Teal accent hairline (brand, not gold chrome).
    draw.rectangle([0, bar_h - 2, w, bar_h], fill=(14, 124, 123))
    if note_lines:
        top = h + bar_h + 4
        draw.rectangle([0, h + bar_h, w, h + bar_h + note_h], fill=(32, 18, 18))
        draw.rectangle([0, h + bar_h, 4, h + bar_h + note_h], fill=(200, 16, 16))
        for i, line in enumerate(note_lines):
            draw.text((10, top + i * 14), line, fill=(255, 236, 236), font=font)
    return _fit_max_edge(canvas, _MAX_EDGE_PX)


def _wrap_note(text: str, max_chars: int) -> list[str]:
    words = text.split()
    if not words:
        return []
    lines: list[str] = []
    cur = words[0]
    for word in words[1:]:
        trial = f"{cur} {word}"
        if len(trial) <= max_chars:
            cur = trial
        else:
            lines.append(cur)
            cur = word
            if len(lines) >= 3:
                break
    if len(lines) < 3:
        lines.append(cur)
    if len(lines) > 3:
        lines = lines[:3]
    if len(lines) == 3 and len(words) > 12:
        lines[-1] = _short(lines[-1], max(8, max_chars - 1))
    return lines


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
                bitmap = page.render(scale=_RENDER_SCALE)
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
