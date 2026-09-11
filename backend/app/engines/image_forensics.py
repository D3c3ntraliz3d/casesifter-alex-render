from __future__ import annotations

import io
from pathlib import Path
from typing import Any

import cv2
import numpy as np
from PIL import Image

from app.config import settings
from app.engines import finding, timed_module
from app.ingestion import extract_embedded_images, load_image, render_pdf_pages
from app.models import Finding, ModuleResult
from app.storage import artifact_dir

def analyze_image_forensics(path: Path, job_id: str) -> ModuleResult:
    return timed_module("image_forensics", lambda: _run(path, job_id))


def _run(path: Path, job_id: str) -> tuple[list[Finding], dict[str, Any]]:
    findings: list[Finding] = []
    artifacts: list[str] = []
    extras: dict[str, Any] = {"pages_analyzed": 0, "artifacts": artifacts}
    images = _collect_images(path)
    extras["pages_analyzed"] = len(images)
    if not images:
        extras["note"] = "No raster images were available for forensic analysis."
        return findings, extras

    out = artifact_dir(job_id)
    for index, (label, image) in enumerate(images, start=1):
        page_no = index if label.startswith("page-") else None
        prefix = f"{label}"

        ela_img, ela_score, ela_hotspots, ela_blob = error_level_analysis(image)
        ela_name = f"{prefix}-ela.png"
        ela_img.save(out / ela_name)
        artifacts.append(ela_name)

        if ela_score >= 18 and ela_blob >= 450 and 0.004 <= ela_hotspots <= 0.18:
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

        overlay, match_count, match_shift = detect_copy_move(image)
        if overlay is not None and match_count >= 12:
            cm_name = f"{prefix}-copymove.png"
            overlay.save(out / cm_name)
            artifacts.append(cm_name)
            findings.append(
                finding(
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
                    evidence={
                        "heatmap": f"/api/jobs/{job_id}/artifacts/{cm_name}",
                        "matches": match_count,
                        "offset": match_shift,
                    },
                    recommended_action="Compare the highlighted regions. Repeated background texture can be a false alarm; repeated numbers or signatures are more serious.",
                )
            )

        dpi_finding = _dpi_and_quality(image, label, page_no, path)
        if dpi_finding:
            findings.append(dpi_finding)

        crop_finding = _aspect_and_crop_hints(image, label, page_no)
        if crop_finding:
            findings.append(crop_finding)

    return findings, extras


def _collect_images(path: Path) -> list[tuple[str, Image.Image]]:
    suffix = path.suffix.lower()
    if suffix in {".jpg", ".jpeg", ".png", ".gif"}:
        return [("image", load_image(path))]
    if suffix == ".pdf":
        pages = render_pdf_pages(path)
        return [(f"page-{i}", img) for i, img in enumerate(pages, start=1)]
    if suffix in {".docx", ".xlsx"}:
        embedded = extract_embedded_images(path)
        return [(f"embedded-{i}-{name.replace('/', '_')}", img) for i, (name, img) in enumerate(embedded, start=1)]
    return []


def error_level_analysis(image: Image.Image, quality: int | None = None, amplify: int | None = None) -> tuple[Image.Image, float, float, int]:
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


def _to_heatmap(mag: np.ndarray) -> Image.Image:
    norm = mag.copy()
    p99 = np.percentile(norm, 99) or 1.0
    norm = np.clip(norm / p99 * 255.0, 0, 255).astype(np.uint8)
    color = cv2.applyColorMap(norm, cv2.COLORMAP_INFERNO)
    color = cv2.cvtColor(color, cv2.COLOR_BGR2RGB)
    return Image.fromarray(color)


def _mask_overlay(image: Image.Image, mask: np.ndarray) -> Image.Image:
    base = np.asarray(image.convert("RGB")).copy()
    if mask.shape[:2] != base.shape[:2]:
        mask = cv2.resize(mask, (base.shape[1], base.shape[0]), interpolation=cv2.INTER_NEAREST)
    tint = base.copy()
    tint[mask > 0] = (0.35 * base[mask > 0] + np.array([220, 40, 40]) * 0.65).astype(np.uint8)
    return Image.fromarray(tint)
