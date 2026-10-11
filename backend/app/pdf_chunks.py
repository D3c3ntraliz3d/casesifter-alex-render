"""Split large PDFs for Gemini Files API — copy page objects as-is (no re-render)."""

from __future__ import annotations

import io
import logging
import tempfile
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

# Defaults match P1-11 product caps.
DEFAULT_CHUNK_MAX_MB = 40
DEFAULT_CHUNK_MAX_PAGES = 300
FILES_API_MAX_MB = 50
FILES_API_MAX_PAGES = 1000


@dataclass(frozen=True)
class PdfChunk:
    path: Path
    page_start: int  # 1-indexed inclusive in the original document
    page_end: int  # 1-indexed inclusive
    size_bytes: int


def pdf_page_count(path: Path) -> int:
    reader = PdfReader(str(path), strict=False)
    return len(reader.pages)


def needs_split(
    path: Path,
    *,
    size_bytes: int | None = None,
    pages: int | None = None,
    max_mb: int = FILES_API_MAX_MB,
    max_pages: int = FILES_API_MAX_PAGES,
) -> bool:
    size = int(size_bytes if size_bytes is not None else path.stat().st_size)
    n_pages = int(pages if pages is not None else pdf_page_count(path))
    return size > max_mb * 1024 * 1024 or n_pages > max_pages


def _write_range(reader: PdfReader, start: int, end: int, dest: Path) -> int:
    """Write pages [start, end) (0-indexed) by copying page objects as-is."""
    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    with open(dest, "wb") as handle:
        writer.write(handle)
    return dest.stat().st_size


def split_pdf(
    path: Path,
    *,
    dest_dir: Path | None = None,
    max_mb: int = DEFAULT_CHUNK_MAX_MB,
    max_pages: int = DEFAULT_CHUNK_MAX_PAGES,
) -> list[PdfChunk]:
    """Split into chunks of at most ``max_mb`` and ``max_pages``.

    Page objects are copied as-is via pypdf (no re-rendering, recompression, or
    downsampling). Temp files are written under ``dest_dir`` (or a new temp dir).
    """
    reader = PdfReader(str(path), strict=False)
    total = len(reader.pages)
    if total < 1:
        raise ValueError("PDF has no pages to split.")

    out_dir = Path(dest_dir) if dest_dir is not None else Path(tempfile.mkdtemp(prefix="cs-pdf-chunks-"))
    out_dir.mkdir(parents=True, exist_ok=True)
    max_bytes = max(1, int(max_mb)) * 1024 * 1024
    max_pages = max(1, int(max_pages))

    chunks: list[PdfChunk] = []
    index = 0
    page_i = 0
    while page_i < total:
        # Largest legal end exclusive for this chunk.
        end = min(total, page_i + max_pages)
        probe = out_dir / f"_probe_{index}.pdf"
        while end > page_i + 1:
            size = _write_range(reader, page_i, end, probe)
            if size <= max_bytes:
                break
            end -= 1
        size = _write_range(reader, page_i, end, probe)
        out_path = out_dir / f"chunk_{index:04d}_p{page_i + 1}-{end}.pdf"
        probe.replace(out_path)
        chunks.append(
            PdfChunk(
                path=out_path,
                page_start=page_i + 1,
                page_end=end,
                size_bytes=out_path.stat().st_size,
            )
        )
        index += 1
        page_i = end

    return chunks


def page_bytes_fingerprint(path: Path, page_index: int) -> bytes:
    """Serialize one page object for byte-identity checks (0-indexed)."""
    reader = PdfReader(str(path), strict=False)
    writer = PdfWriter()
    writer.add_page(reader.pages[page_index])
    buf = io.BytesIO()
    writer.write(buf)
    return buf.getvalue()
