"""Split large PDFs for Gemini Files API — copy page objects as-is (no re-render)."""

from __future__ import annotations

import io
import logging
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path

from pypdf import PdfReader, PdfWriter

logger = logging.getLogger(__name__)

# Defaults match P1-11 product caps.
DEFAULT_CHUNK_MAX_MB = 40
DEFAULT_CHUNK_MAX_PAGES = 300
FILES_API_MAX_MB = 50
FILES_API_MAX_PAGES = 1000

# Only one split at a time in the web process (pypdf + temp disk is heavy).
_SPLIT_LOCK = threading.Lock()

# Cumulative bytes written by ``_write_range`` (disk finals only). Tests reset/read this.
_write_range_bytes_total = 0
_write_range_bytes_lock = threading.Lock()


def reset_write_range_bytes() -> None:
    global _write_range_bytes_total
    with _write_range_bytes_lock:
        _write_range_bytes_total = 0


def get_write_range_bytes() -> int:
    with _write_range_bytes_lock:
        return int(_write_range_bytes_total)


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
    global _write_range_bytes_total
    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    with open(dest, "wb") as handle:
        writer.write(handle)
    size = dest.stat().st_size
    with _write_range_bytes_lock:
        _write_range_bytes_total += size
    return size


def _range_size_memory(reader: PdfReader, start: int, end: int) -> int:
    """Serialized size of pages [start, end) in memory — used for binary search only."""
    writer = PdfWriter()
    for i in range(start, end):
        writer.add_page(reader.pages[i])
    buf = io.BytesIO()
    writer.write(buf)
    return int(buf.tell())


def _fit_page_count(
    reader: PdfReader,
    start: int,
    hard_end: int,
    *,
    max_bytes: int,
    avg_bytes_per_page: float,
) -> int:
    """Largest page count in [1, hard_end-start] that fits ``max_bytes`` (binary search)."""
    max_count = hard_end - start
    if max_count <= 1:
        return max(1, max_count)

    # Seed the search window from bytes-per-page estimate.
    if avg_bytes_per_page > 0:
        estimate = max(1, min(max_count, int(max_bytes / avg_bytes_per_page)))
    else:
        estimate = max_count

    lo, hi = 1, max_count
    best = 1
    # First probe near the estimate to cut iterations on uniform PDFs.
    for mid in (estimate,):
        sz = _range_size_memory(reader, start, start + mid)
        if sz <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1

    while lo <= hi:
        mid = (lo + hi) // 2
        sz = _range_size_memory(reader, start, start + mid)
        if sz <= max_bytes:
            best = mid
            lo = mid + 1
        else:
            hi = mid - 1
    return best


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

    Serialized: only one ``split_pdf`` runs at a time in this process. Each chunk
    is written to disk once (binary-search sizing uses in-memory probes).
    """
    with _SPLIT_LOCK:
        reader = PdfReader(str(path), strict=False)
        total = len(reader.pages)
        if total < 1:
            raise ValueError("PDF has no pages to split.")

        out_dir = (
            Path(dest_dir) if dest_dir is not None else Path(tempfile.mkdtemp(prefix="cs-pdf-chunks-"))
        )
        out_dir.mkdir(parents=True, exist_ok=True)
        max_bytes = max(1, int(max_mb)) * 1024 * 1024
        max_pages = max(1, int(max_pages))
        try:
            source_size = int(path.stat().st_size)
        except OSError:
            source_size = 0
        avg = (source_size / total) if total else float(source_size)

        chunks: list[PdfChunk] = []
        index = 0
        page_i = 0
        while page_i < total:
            hard_end = min(total, page_i + max_pages)
            count = _fit_page_count(
                reader,
                page_i,
                hard_end,
                max_bytes=max_bytes,
                avg_bytes_per_page=avg,
            )
            end = page_i + count
            out_path = out_dir / f"chunk_{index:04d}_p{page_i + 1}-{end}.pdf"
            size = _write_range(reader, page_i, end, out_path)
            chunks.append(
                PdfChunk(
                    path=out_path,
                    page_start=page_i + 1,
                    page_end=end,
                    size_bytes=size,
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
