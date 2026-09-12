from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

from PIL import Image, ImageSequence

from app.config import settings

Image.MAX_IMAGE_PIXELS = 80_000_000

IMAGE_EXTS = {".jpg", ".jpeg", ".png", ".gif"}
RENDERABLE_EXTS = IMAGE_EXTS | {".pdf"}


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def sniff_type(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix in settings.allowed_extensions:
        return suffix.lstrip(".")
    return "unknown"


def load_image(path: Path) -> Image.Image:
    image = Image.open(path)
    if path.suffix.lower() == ".gif":
        image = ImageSequence.Iterator(image)[0]
    if image.mode not in ("RGB", "L"):
        image = image.convert("RGB")
    elif image.mode == "L":
        image = image.convert("RGB")
    return image


def render_pdf_pages(path: Path, max_pages: int | None = None, scale: float | None = None):
    return list(iter_pdf_pages(path, max_pages=max_pages, scale=scale))


def iter_pdf_pages(path: Path, max_pages: int | None = None, scale: float | None = None):
    """Yield one rendered page at a time so large exhibits do not stay in RAM."""
    import pypdfium2 as pdfium

    scale = scale if scale is not None else settings.render_scale
    max_pages = max_pages if max_pages is not None else settings.max_forensic_pages
    doc = pdfium.PdfDocument(str(path))
    try:
        count = min(len(doc), max_pages)
        for index in range(count):
            page = doc[index]
            try:
                bitmap = page.render(scale=scale)
                yield bitmap.to_pil().convert("RGB")
            finally:
                page.close()
    finally:
        doc.close()


def extract_embedded_images(path: Path) -> list[tuple[str, Image.Image]]:
    """Pull raster images out of Office packages for forensic review."""
    suffix = path.suffix.lower()
    if suffix not in {".docx", ".xlsx"}:
        return []
    images: list[tuple[str, Image.Image]] = []
    media_prefix = "word/media/" if suffix == ".docx" else "xl/media/"
    try:
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if not name.startswith(media_prefix):
                    continue
                lower = name.lower()
                if not lower.endswith((".png", ".jpg", ".jpeg", ".gif", ".tif", ".tiff", ".bmp")):
                    continue
                raw = archive.read(name)
                try:
                    img = Image.open(io.BytesIO(raw)).convert("RGB")
                    images.append((name, img))
                except Exception:
                    continue
    except zipfile.BadZipFile:
        return []
    return images[:20]


def open_pdf_reader(path: Path):
    from pypdf import PdfReader

    reader = PdfReader(str(path), strict=False)
    if reader.is_encrypted:
        try:
            reader.decrypt("")
        except Exception:
            pass
    return reader
