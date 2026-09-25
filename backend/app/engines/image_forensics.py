from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import numpy as np
from PIL import Image

from app.config import settings
from app.engines import finding, timed_module
from app.ingestion import extract_embedded_images, iter_pdf_pages, load_image
from app.models import Finding, ModuleResult
from app.storage import artifact_dir


def _cv2():
    """Lazy import — keep analysis API bootable if OpenCV wheels misbehave on slim images."""
    import cv2

    return cv2


def analyze_image_forensics(
    path: Path,
    job_id: str,
    origin: dict[str, Any] | None = None,
) -> ModuleResult:
    return timed_module("image_forensics", lambda: _run(path, job_id, origin or {}))


def _run(
    path: Path, job_id: str, origin: dict[str, Any]
) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    artifacts: list[str] = []
    # Court exhibits are almost always scans or photos of paper. An OCR text layer does
    # not make a PDF "digitally born" — only a native-text PDF with no page rasters does.
    suffix = path.suffix.lower()
    raster_pages = int(origin.get("raster_pages") or 0)
    digitally_born_pdf = (
        suffix == ".pdf"
        and origin.get("guess") == "native-text"
        and not origin.get("scan_heavy")
        and raster_pages == 0
    )
    scan_heavy = (
        bool(origin.get("scan_heavy"))
        or origin.get("guess") in {"image-only", "mostly-scanned", "scanned", "hybrid"}
        or suffix in {".jpg", ".jpeg", ".png", ".gif"}
        or (suffix == ".pdf" and not digitally_born_pdf)
    )
    extras: dict[str, Any] = {
        "pages_analyzed": 0,
        "artifacts": artifacts,
        "scan_heavy": scan_heavy,
        "origin_guess": origin.get("guess"),
    }
    images = _collect_images(path)
    extras["pages_analyzed"] = 0
    extras["forensic_budget"] = _forensic_budget(path)
    out = artifact_dir(job_id)
    analyzed = 0
    for index, (label, image) in enumerate(images, start=1):
        analyzed += 1
        extras["pages_analyzed"] = analyzed
        page_no = index if label.startswith("page-") else None
        prefix = f"{label}"

        ela_img, ela_score, ela_hotspots, ela_blob = error_level_analysis(image)
        ela_name = f"{prefix}-ela.png"
        ela_img.save(out / ela_name)
        artifacts.append(ela_name)

        if ela_score >= 18 and ela_blob >= 450 and 0.004 <= ela_hotspots <= 0.18:
            if scan_heavy:
                # Scanned pages re-JPEG differently across the sheet; keep as a soft note only
                # when the signal is extreme — clean synthetic decoys must stay quiet.
                if ela_score >= 62 and ela_blob >= 2200:
                    findings.append(
                        finding(
                            fid=f"img-ela-{prefix}",
                            category="image",
                            severity="info",
                            title=f"Uneven recompression on scanned page {label}",
                            explanation=(
                                f"Error Level Analysis found localized patches (score {ela_score:.1f}). "
                                "On a scanned exhibit this is often scanner JPEG texture, not a paste. "
                                "Worth a glance at the heatmap only if a specific field looks retouched."
                            ),
                            why_suspicious=(
                                "Weak signal on scan-heavy PDFs. Digitally born photos with sharp ELA "
                                "edges remain more meaningful."
                            ),
                            confidence=32,
                            page=page_no,
                            evidence={
                                "heatmap": f"/api/jobs/{job_id}/artifacts/{ela_name}",
                                "ela_score": round(ela_score, 2),
                                "hotspot_ratio": round(ela_hotspots, 4),
                                "largest_blob_px": ela_blob,
                                "scan_heavy": True,
                            },
                        )
                    )
            else:
                findings.append(
                    finding(
                        fid=f"img-ela-{prefix}",
                        category="image",
                        severity="high" if ela_score >= 40 else "medium",
                        title=f"Uneven JPEG recompression in {label}",
                        explanation=(
                            f"Error Level Analysis found localized patches that recompress differently "
                            f"(score {ela_score:.1f}, hotspot coverage {ela_hotspots:.1%}). "
                            "That pattern often appears when part of an image was pasted from another photo."
                        ),
                        why_suspicious="Spliced regions usually have a different JPEG history than the rest of the frame.",
                        confidence=min(92, 50 + ela_score),
                        page=page_no,
                        evidence={
                            "heatmap": f"/api/jobs/{job_id}/artifacts/{ela_name}",
                            "ela_score": round(ela_score, 2),
                            "hotspot_ratio": round(ela_hotspots, 4),
                            "largest_blob_px": ela_blob,
                        },
                        recommended_action="Inspect the ELA heatmap. Bright regions deserve a closer look, not an automatic fraud finding.",
                    )
                )

        overlay, match_count, match_shift = (None, 0, (0, 0))
        if not scan_heavy:
            overlay, match_count, match_shift = detect_copy_move(image)
        clone_finding = _copy_move_finding(
            label=label,
            page_no=page_no,
            job_id=job_id,
            prefix=prefix,
            overlay=overlay,
            match_count=match_count,
            match_shift=match_shift,
            scan_heavy=scan_heavy,
            out=out,
            artifacts=artifacts,
        )
        if clone_finding:
            findings.append(clone_finding)

        dpi_finding = _dpi_and_quality(image, label, page_no, path)
        if dpi_finding:
            findings.append(dpi_finding)

        crop_finding = _aspect_and_crop_hints(image, label, page_no)
        if crop_finding:
            findings.append(crop_finding)

        overlay_finding = _solid_overlay_finding(
            image, label=label, page_no=page_no, job_id=job_id, prefix=prefix, out=out, artifacts=artifacts
        )
        if overlay_finding:
            findings.append(overlay_finding)
        image.close()

    if analyzed == 0:
        extras["note"] = "No raster images were available for forensic analysis."
    return findings, extras


def _copy_move_finding(
    *,
    label: str,
    page_no: int | None,
    job_id: str,
    prefix: str,
    overlay: Image.Image | None,
    match_count: int,
    match_shift: tuple[int, int],
    scan_heavy: bool,
    out: Path,
    artifacts: list[str],
) -> Finding | None:
    if overlay is None or match_count < 12:
        return None

    cm_name = f"{prefix}-copymove.png"
    overlay.save(out / cm_name)
    artifacts.append(cm_name)
    evidence = {
        "heatmap": f"/api/jobs/{job_id}/artifacts/{cm_name}",
        "matches": match_count,
        "offset": match_shift,
        "scan_heavy": scan_heavy,
    }

    # Pure horizontal/vertical offsets are extremely common on scanned forms (ruled lines,
    # letterhead repeats, column structure). Treat those as packaging noise on scans.
    axis_aligned = match_shift[0] == 0 or match_shift[1] == 0

    if scan_heavy:
        # Default: do not raise clone warnings on scanned exhibits.
        # Only surface an informational note when the vote count is extreme and not a
        # simple form-line shift.
        if match_count < 55 or axis_aligned:
            return None
        return finding(
            fid=f"img-copymove-{prefix}",
            category="image",
            severity="info",
            title=f"Repeated blocks on scanned page {label}",
            explanation=(
                f"Copy-move analysis found {match_count} matching blocks offset by about "
                f"{match_shift} pixels. This file looks like a scan of paper. Matching blocks "
                "on scans usually come from form lines, stamps, or JPEG texture — not cloning. "
                "Check the overlay only if a signature, amount, or stamp looks duplicated by eye."
            ),
            why_suspicious=(
                "Not a primary fraud signal for scanned court exhibits. Digitally born photos "
                "with cloned patches remain more meaningful."
            ),
            confidence=28,
            page=page_no,
            evidence=evidence,
            recommended_action=(
                "Ignore unless the highlighted regions are clearly the same signature, stamp, "
                "or dollar amount pasted twice."
            ),
        )

    return finding(
        fid=f"img-copymove-{prefix}",
        category="image",
        severity="high",
        title=f"Repeated (cloned) regions in {label}",
        explanation=(
            f"Copy-move analysis found {match_count} matching blocks offset by "
            f"about {match_shift} pixels. Cloning is used to hide content or duplicate stamps/signatures."
        ),
        why_suspicious="Natural photographs rarely contain large duplicated pixel patches at a consistent offset.",
        confidence=min(90, 55 + match_count),
        page=page_no,
        evidence=evidence,
        recommended_action=(
            "Compare the highlighted regions. Repeated background texture can be a false alarm; "
            "repeated numbers or signatures are more serious."
        ),
    )


def _forensic_budget(path: Path) -> dict[str, Any]:
    size = path.stat().st_size if path.exists() else 0
    mb = size / (1024 * 1024)
    if mb >= 12:
        return {"max_pages": 6, "scale": 1.0}
    if mb >= 4:
        return {"max_pages": 8, "scale": 1.0}
    return {"max_pages": settings.max_forensic_pages, "scale": settings.render_scale}


def _collect_images(path: Path):
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif"}:
        yield "image", load_image(path)
        return
    if suffix == ".pdf":
        budget = _forensic_budget(path)
        for i, img in enumerate(
            iter_pdf_pages(path, max_pages=budget["max_pages"], scale=budget["scale"]),
            start=1,
        ):
            yield f"page-{i}", img
        return
    if suffix in {".docx", ".xlsx"}:
        embedded = extract_embedded_images(path)
        for i, (name, img) in enumerate(embedded, start=1):
            yield f"embedded-{i}-{name.replace('/', '_')}", img


def error_level_analysis(image: Image.Image, quality: int | None = None, amplify: int | None = None) -> tuple[Image.Image, float, float, int]:
    cv2 = _cv2()
    quality = quality or settings.ela_quality
    amplify = amplify or settings.ela_scale
    working = image.convert("RGB")
    buffer = io.BytesIO()
    working.save(buffer, format="JPEG", quality=quality)
    buffer.seek(0)
    recompressed = Image.open(buffer).convert("RGB")
    orig = np.asarray(working, dtype=np.int16)
    rec = np.asarray(recompressed, dtype=np.int16)
    diff = np.abs(orig - rec).astype(np.float32)
    mag = diff.mean(axis=2)
    # Ignore a thin outer border; scanners often have noisy edges.
    if mag.shape[0] > 40 and mag.shape[1] > 40:
        inner = mag[20:-20, 20:-20]
    else:
        inner = mag
    median = float(np.median(inner)) if inner.size else 0.0
    p95 = float(np.percentile(inner, 95)) if inner.size else 0.0
    score = max(0.0, p95 - median)
    threshold = max(median * 3.2, median + 16.0, 18.0)
    hot = (inner > threshold).astype(np.uint8)
    hotspot_ratio = float(hot.mean()) if hot.size else 0.0
    max_blob = 0
    if hot.any():
        closed = cv2.morphologyEx(hot, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
        n_labels, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
        if n_labels > 1:
            max_blob = int(stats[1:, cv2.CC_STAT_AREA].max())
    heatmap = _to_heatmap(mag)
    blended = Image.blend(working, heatmap.convert("RGB"), 0.55)
    return blended, score, hotspot_ratio, max_blob


def detect_copy_move(image: Image.Image, max_side: int = 640, block: int = 16, stride: int = 12):
    cv2 = _cv2()
    rgb = np.array(image.convert("RGB"))
    h0, w0 = rgb.shape[:2]
    scale = min(1.0, max_side / max(h0, w0))
    small = cv2.resize(rgb, (int(w0 * scale), int(h0 * scale)), interpolation=cv2.INTER_AREA) if scale < 1 else rgb
    gray = cv2.cvtColor(small, cv2.COLOR_RGB2GRAY)
    h, w = gray.shape
    if h < block * 4 or w < block * 4:
        return None, 0, (0, 0)

    features: dict[bytes, list[tuple[int, int]]] = {}
    for y in range(0, h - block, stride):
        for x in range(0, w - block, stride):
            patch = gray[y : y + block, x : x + block]
            if float(patch.var()) < 40:  # skip blank paper / sky
                continue
            dct = cv2.dct(np.float32(patch))
            key = np.round(dct[:3, :3] / 8.0).astype(np.int8).tobytes()
            features.setdefault(key, []).append((x, y))

    mask = np.zeros((h, w), dtype=np.uint8)
    offsets: dict[tuple[int, int], int] = {}
    min_dist = block * 3
    for positions in features.values():
        if len(positions) < 2 or len(positions) > 6:
            continue
        for i, (x1, y1) in enumerate(positions):
            for x2, y2 in positions[i + 1 :]:
                dx, dy = x2 - x1, y2 - y1
                if dx * dx + dy * dy < min_dist * min_dist:
                    continue
                # Quantize offset so cloned regions vote together.
                qdx, qdy = int(round(dx / stride) * stride), int(round(dy / stride) * stride)
                offsets[(qdx, qdy)] = offsets.get((qdx, qdy), 0) + 1
                cv2.rectangle(mask, (x1, y1), (x1 + block, y1 + block), 255, -1)
                cv2.rectangle(mask, (x2, y2), (x2 + block, y2 + block), 255, -1)

    if not offsets:
        return None, 0, (0, 0)
    best_offset, votes = max(offsets.items(), key=lambda item: item[1])
    if votes < 8:
        return None, votes, best_offset

    mask = cv2.dilate(mask, np.ones((5, 5), np.uint8), iterations=1)
    if scale < 1:
        mask = cv2.resize(mask, (w0, h0), interpolation=cv2.INTER_NEAREST)
    heat = _mask_overlay(image, mask)
    return heat, int(votes), (int(best_offset[0] / scale), int(best_offset[1] / scale))


def _dpi_and_quality(image: Image.Image, label: str, page: int | None, path: Path) -> Finding | None:
    dpi = image.info.get("dpi")
    if not dpi and path.suffix.lower() in {".jpg", ".jpeg", ".png"}:
        dpi = Image.open(path).info.get("dpi")
    if not dpi:
        return None
    try:
        xdpi = float(dpi[0] if isinstance(dpi, tuple) else dpi)
    except (TypeError, ValueError):
        return None
    if xdpi < 50:
        return finding(
            fid=f"img-lowdpi-{label}",
            category="image",
            severity="low",
            title=f"Very low recorded DPI in {label}",
            explanation=f"Embedded resolution is {xdpi:.0f} DPI. Fine print and signatures may have been downsampled.",
            why_suspicious="Downsampling can hide splicing artifacts and is sometimes used after editing.",
            confidence=48,
            page=page,
            evidence={"dpi": xdpi},
        )
    return None


def _aspect_and_crop_hints(image: Image.Image, label: str, page: int | None) -> Finding | None:
    arr = np.asarray(image.convert("L"))
    # Detect a strong single-color border that suggests cropping from a larger page.
    h, w = arr.shape
    if h < 80 or w < 80:
        return None
    border = np.concatenate(
        [arr[:8, :].ravel(), arr[-8:, :].ravel(), arr[:, :8].ravel(), arr[:, -8:].ravel()]
    )
    interior = arr[h // 4 : 3 * h // 4, w // 4 : 3 * w // 4]
    if border.std() < 6 and abs(float(border.mean()) - float(interior.mean())) > 45:
        return finding(
            fid=f"img-crop-{label}",
            category="image",
            severity="info",
            title=f"Uniform border suggests cropping in {label}",
            explanation="A flat-color frame around a contrasting interior often means the image was cropped or padded.",
            why_suspicious="Cropping can remove letterhead, timestamps, or other context from an exhibit.",
            confidence=40,
            page=page,
        )
    return None


def _solid_overlay_finding(
    image: Image.Image,
    *,
    label: str,
    page_no: int | None,
    job_id: str,
    prefix: str,
    out: Path,
    artifacts: list[str],
) -> Finding | None:
    """Flag a large near-white solid rectangle pasted over page content.

    Classic ELA is near-zero on PNG/flat synthetic whiteouts. A contiguous *pure*
    white block (brighter and flatter than the paper background) covering mid-page
    body is the scan-survivable whiteout / redaction paste signal on utility bills.
    """
    cv2 = _cv2()
    rgb = np.asarray(image.convert("RGB"))
    h, w = rgb.shape[:2]
    if h < 120 or w < 120:
        return None
    gray = cv2.cvtColor(rgb, cv2.COLOR_RGB2GRAY)
    # Whole-page blank / near-blank sheets are not "whiteout over content".
    if float(np.mean(gray > 245)) > 0.88:
        return None
    yi, ya = int(h * 0.10), int(h * 0.92)
    xi, xa = int(w * 0.06), int(w * 0.94)
    interior = gray[yi:ya, xi:xa]
    paper = float(np.median(interior))
    # Pure paste sits above typical paper gray (often ~240–248) at 253–255.
    pure = (interior >= max(253, int(paper) + 6)).astype(np.uint8) * 255
    pure_frac = float(pure.mean()) / 255.0
    if pure_frac < 0.02 or pure_frac > 0.55:
        return None
    closed = cv2.morphologyEx(pure, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n_labels, _, stats, _ = cv2.connectedComponentsWithStats(closed, connectivity=8)
    if n_labels <= 1:
        return None
    areas = stats[1:, cv2.CC_STAT_AREA]
    idx = 1 + int(np.argmax(areas))
    x, y, bw, bh, area = (int(v) for v in stats[idx])
    interior_area = max(1, interior.size)
    frac = area / interior_area
    # Mid-sized body block: too small = a few glyphs; too large = blank page.
    if frac < 0.03 or frac > 0.45:
        return None
    if bw < w * 0.25 or bh < max(18, int(h * 0.025)):
        return None
    # Require ink above or below the band so paper margins never fire.
    above = interior[max(0, y - 16) : y, x : x + bw]
    below = interior[y + bh : min(interior.shape[0], y + bh + 16), x : x + bw]
    touch = False
    for strip in (above, below):
        if strip.size == 0:
            continue
        if float(strip.mean()) < paper - 6 or float(strip.std()) >= 8:
            touch = True
            break
    if not touch:
        return None

    mask = np.zeros((h, w), dtype=np.uint8)
    mask[yi + y : yi + y + bh, xi + x : xi + x + bw] = 255
    overlay = _mask_overlay(image, mask)
    name = f"{prefix}-solid-overlay.png"
    overlay.save(out / name)
    artifacts.append(name)
    return finding(
        fid=f"img-solid-overlay-{prefix}",
        category="image",
        severity="high",
        title=f"Solid white overlay covering content on {label}",
        explanation=(
            f"A near-uniform pure-white rectangle (~{frac:.0%} of the page body, {bw}×{bh}px) "
            "sits over darker surrounding print. That pattern matches a whiteout / paste / "
            "redaction block on a scanned utility or bank statement, not ordinary paper margin."
        ),
        why_suspicious=(
            "Authentic issuer statements are rendered as one job. A flat white slab over "
            "account lines, amounts, or addresses is the usual way an edited field is hidden "
            "before print-then-scan."
        ),
        confidence=78,
        page=page_no,
        evidence={
            "analysis_type": "graphic_manipulation",
            "heatmap": f"/api/jobs/{job_id}/artifacts/{name}",
            "overlay_frac": round(frac, 4),
            "overlay_box_px": [bw, bh],
            "print_scan_review": True,
            "content_hook": True,
        },
        recommended_action=(
            "Open the overlay highlight. Compare the covered band to a known-authentic bill "
            "from the same issuer — look for missing amounts, account digits, or service address."
        ),
    )


def _to_heatmap(mag: np.ndarray) -> Image.Image:
    cv2 = _cv2()
    norm = mag.copy()
    p99 = np.percentile(norm, 99) or 1.0
    norm = np.clip(norm / p99 * 255.0, 0, 255).astype(np.uint8)
    color = cv2.applyColorMap(norm, cv2.COLORMAP_INFERNO)
    color = cv2.cvtColor(color, cv2.COLOR_BGR2RGB)
    return Image.fromarray(color)


def _mask_overlay(image: Image.Image, mask: np.ndarray) -> Image.Image:
    cv2 = _cv2()
    base = np.asarray(image.convert("RGB")).copy()
    if mask.shape[:2] != base.shape[:2]:
        mask = cv2.resize(mask, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_NEAREST)
    tint = base.copy()
    tint[mask > 0] = (0.35 * base[mask > 0] + np.array([220, 40, 40]) * 0.65).astype(np.uint8)
    return Image.fromarray(tint)
