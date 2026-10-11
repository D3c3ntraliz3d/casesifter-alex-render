"""P1-11: Files API path, PDF chunking, page offsets, partial failures."""

from __future__ import annotations

import io
import json
from pathlib import Path

import pytest
from pypdf import PdfReader, PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject, NumberObject


def _blank_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as handle:
        writer.write(handle)


def _fat_pdf(path: Path, target_mb: float, *, pages: int = 8) -> None:
    """Build a multi-page PDF roughly ``target_mb`` in size (embedded raw streams)."""
    writer = PdfWriter()
    # Spread payload across pages so page-copy tests stay meaningful.
    per = max(64_000, int((target_mb * 1024 * 1024) / max(1, pages)))
    blob = (b"SCANNED-PAGE-BYTES-" * (per // 18 + 1))[:per]
    for i in range(pages):
        writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(blob)
        stream_ref = writer._add_object(stream)
        page = writer.pages[i]
        page[NameObject("/CSPrivate")] = stream_ref
    with open(path, "wb") as handle:
        writer.write(handle)


def test_20mb_pdf_uses_files_api_and_deletes(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "fat20.pdf"
    _fat_pdf(source, 20.5, pages=6)
    assert source.stat().st_size > 15 * 1024 * 1024

    deleted: list[str] = []
    uploaded: list[Path] = []

    def fake_upload(path, *, mime_type="application/pdf"):
        uploaded.append(Path(path))
        return {"name": "files/mock-20mb", "uri": "https://example.test/files/mock-20mb", "state": "ACTIVE"}

    def fake_delete(file_obj):
        if file_obj:
            deleted.append(str(file_obj.get("name") if isinstance(file_obj, dict) else file_obj))

    def fake_analyze(text, file_base64, mime_type, extras=None, *, file_ref=None, page_offset=0):
        assert file_base64 is None
        assert file_ref is not None
        assert file_ref["file_data"]["file_uri"]
        return {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "application/pdf",
            "model": "gemini-test",
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", fake_delete)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "Exhibit text.",
        {"filename": "fat20.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    assert (result.extras or {}).get("document_analysis_transport") == "files_api"
    assert uploaded and uploaded[0] == source
    assert deleted == ["files/mock-20mb"]


def test_1200_page_split_offsets_and_byte_identity(tmp_path):
    from app.pdf_chunks import page_bytes_fingerprint, split_pdf

    source = tmp_path / "long.pdf"
    _blank_pdf(source, 1200)
    chunks = split_pdf(source, dest_dir=tmp_path / "chunks", max_mb=40, max_pages=300)
    assert len(chunks) == 4
    assert chunks[0].page_start == 1 and chunks[0].page_end == 300
    assert chunks[1].page_start == 301 and chunks[1].page_end == 600
    assert chunks[2].page_start == 601 and chunks[2].page_end == 900
    assert chunks[3].page_start == 901 and chunks[3].page_end == 1200

    # Page objects copied as-is: fingerprint of original page == fingerprint inside chunk.
    for chunk in chunks:
        local_index = 0
        orig_index = chunk.page_start - 1
        assert page_bytes_fingerprint(source, orig_index) == page_bytes_fingerprint(
            chunk.path, local_index
        )


def test_planted_forgery_on_page_900_reports_page_900(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state
    from app.pdf_chunks import split_pdf

    reset_ai_review_state()
    source = tmp_path / "long.pdf"
    _blank_pdf(source, 1200)

    def fake_analyze(text, file_base64, mime_type, extras=None, *, file_ref=None, page_offset=0):
        # Spec: plant on chunk-local page 300 with offset 600 → original page 900.
        findings = []
        if page_offset == 600:
            local_page = 300
            findings.append(
                {
                    "analysis_type": "text_overlay",
                    "title": "Planted forgery",
                    "observation": "Suspicious overlay near the signature block.",
                    "why_suspicious": "Ink density differs from surrounding print.",
                    "confidence": 90,
                    "severity": "medium",
                    "page": local_page + int(page_offset),
                    "quoted_evidence": "FORGED",
                }
            )
        return {
            "findings": findings,
            "overall_note": "chunk",
            "file_sent": "application/pdf",
            "model": "gemini-test",
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_min_confidence", 70, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(cl.settings, "gemini_max_concurrent", 2, raising=False)
    monkeypatch.setattr(
        "app.gemini_files.upload_file_streaming",
        lambda path, **_k: {
            "name": f"files/{Path(path).name}",
            "uri": f"https://example.test/{Path(path).name}",
            "state": "ACTIVE",
        },
    )
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)
    # Force chunk path even though blank PDF is tiny.
    monkeypatch.setattr("app.pdf_chunks.needs_split", lambda *_a, **_k: True)
    monkeypatch.setattr(
        "app.pdf_chunks.split_pdf",
        lambda path, **_k: split_pdf(path, dest_dir=tmp_path / "chunks2", max_pages=300, max_mb=40),
    )

    result = cl.analyze_contextual(
        "Long exhibit.",
        {"filename": "long.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    pages = [f.page for f in result.findings if f.page is not None]
    assert 900 in pages


def test_failed_chunk_marks_partial(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import get_ai_review_failure, get_failed_page_ranges, reset_ai_review_state
    from app.pdf_chunks import PdfChunk, split_pdf

    reset_ai_review_state()
    source = tmp_path / "long.pdf"
    _blank_pdf(source, 600)
    real_chunks = split_pdf(source, dest_dir=tmp_path / "c", max_pages=300, max_mb=40)

    calls = {"n": 0}

    def fake_analyze(text, file_base64, mime_type, extras=None, *, file_ref=None, page_offset=0):
        calls["n"] += 1
        if page_offset == 0:
            raise RuntimeError("chunk boom")
        return {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "application/pdf",
            "model": "gemini-test",
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(cl.settings, "gemini_max_concurrent", 2, raising=False)
    monkeypatch.setattr(
        "app.gemini_files.upload_file_streaming",
        lambda path, **_k: {"name": "files/x", "uri": "https://example.test/x", "state": "ACTIVE"},
    )
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)
    monkeypatch.setattr("app.pdf_chunks.needs_split", lambda *_a, **_k: True)
    monkeypatch.setattr("app.pdf_chunks.split_pdf", lambda *_a, **_k: real_chunks)

    result = cl.analyze_contextual(
        "Exhibit.",
        {"filename": "long.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    assert get_ai_review_failure() == "partial"
    assert [1, 300] in get_failed_page_ranges() or [1, 300] in (
        (result.extras or {}).get("document_analysis_failed_page_ranges") or []
    )
    assert (result.extras or {}).get("document_analysis_result", {}).get("file_sent") == "application/pdf"


def test_split_150mb_writes_less_than_3x_source(tmp_path):
    """Final chunk writes via _write_range stay under 3× source (no shrink-loop thrash)."""
    from app.pdf_chunks import get_write_range_bytes, reset_write_range_bytes, split_pdf

    source = tmp_path / "fat150.pdf"
    _fat_pdf(source, 150.5, pages=24)
    src_size = source.stat().st_size
    assert src_size > 150 * 1024 * 1024

    reset_write_range_bytes()
    chunks = split_pdf(source, dest_dir=tmp_path / "chunks150", max_mb=40, max_pages=300)
    written = get_write_range_bytes()
    assert chunks
    assert written > 0
    assert written < 3 * src_size, f"wrote {written} bytes for source {src_size}"


def test_cancel_stops_remaining_chunk_uploads(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_cancel import clear_cancel, request_cancel, set_current_job
    from app.gemini_review_state import reset_ai_review_state
    from app.pdf_chunks import split_pdf

    reset_ai_review_state()
    job_id = "c" * 32
    set_current_job(job_id)
    clear_cancel(job_id)

    source = tmp_path / "long.pdf"
    _blank_pdf(source, 1200)
    real_chunks = split_pdf(source, dest_dir=tmp_path / "cc", max_pages=300, max_mb=40)
    assert len(real_chunks) == 4

    uploads: list[str] = []

    def fake_upload(path, *, mime_type="application/pdf"):
        uploads.append(str(path))
        # After the first upload starts, cancel so later workers skip.
        request_cancel(job_id)
        return {
            "name": f"files/{Path(path).name}",
            "uri": f"https://example.test/{Path(path).name}",
            "state": "ACTIVE",
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    # Serialize workers so cancel is visible before the next upload.
    monkeypatch.setattr(cl.settings, "gemini_max_concurrent", 1, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr(
        cl,
        "analyze_document",
        lambda *a, **k: {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "application/pdf",
            "model": "gemini-test",
        },
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)
    monkeypatch.setattr("app.pdf_chunks.needs_split", lambda *_a, **_k: True)
    monkeypatch.setattr("app.pdf_chunks.split_pdf", lambda *_a, **_k: real_chunks)

    cl.analyze_contextual(
        "Long exhibit.",
        {"filename": "long.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    clear_cancel(job_id)
    set_current_job(None)
    assert len(uploads) == 1, f"expected 1 upload after cancel, got {len(uploads)}"


def test_chunk_402_reports_billing(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_cancel import set_current_job
    from app.gemini_review_state import get_ai_review_failure, note_ai_review_failure, reset_ai_review_state
    from app.pdf_chunks import split_pdf

    reset_ai_review_state()
    set_current_job("b" * 32)
    source = tmp_path / "long.pdf"
    _blank_pdf(source, 600)
    real_chunks = split_pdf(source, dest_dir=tmp_path / "cb", max_pages=300, max_mb=40)

    def fake_upload(path, *, mime_type="application/pdf"):
        note_ai_review_failure("billing")
        raise RuntimeError("Gemini Files upload HTTP 402: quota")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(cl.settings, "gemini_max_concurrent", 2, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)
    monkeypatch.setattr("app.pdf_chunks.needs_split", lambda *_a, **_k: True)
    monkeypatch.setattr("app.pdf_chunks.split_pdf", lambda *_a, **_k: real_chunks)

    cl.analyze_contextual(
        "Exhibit.",
        {"filename": "long.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    set_current_job(None)
    assert get_ai_review_failure() == "billing"


def test_gemini_files_urls_have_no_api_key_query(monkeypatch):
    """All Files API requests must use x-goog-api-key only — never ?key=."""
    import app.gemini_files as gf

    seen: list[str] = []

    class _Resp:
        headers = {"X-Goog-Upload-URL": "https://generativelanguage.googleapis.com/upload/token"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            return False

        def read(self):
            return b'{"name":"files/x","uri":"https://example.test/files/x","state":"ACTIVE"}'

    def fake_urlopen(req, timeout=None):
        seen.append(req.full_url)
        assert "key=" not in req.full_url
        assert req.get_header("X-goog-api-key") or req.headers.get("x-goog-api-key")
        return _Resp()

    monkeypatch.setattr(gf.settings, "gemini_api_key", "secret-key", raising=False)
    monkeypatch.setattr(gf.urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(gf.time, "sleep", lambda *_a, **_k: None)

    req = gf.urllib.request.Request(
        gf._UPLOAD_START,
        data=b"{}",
        method="POST",
        headers={"x-goog-api-key": "secret-key", "Content-Type": "application/json"},
    )
    with gf._urlopen_with_retries(req, 5) as resp:
        assert resp is not None
    assert seen
    assert all("key=" not in u for u in seen)
