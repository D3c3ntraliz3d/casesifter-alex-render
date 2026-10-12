"""Large-exhibit Task D: page images, selected-pages Files API, pdf_chunks helpers."""

from __future__ import annotations

from pathlib import Path

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, NameObject


def _blank_pdf(path: Path, pages: int) -> None:
    writer = PdfWriter()
    for _ in range(pages):
        writer.add_blank_page(width=612, height=792)
    with open(path, "wb") as handle:
        writer.write(handle)


def _fat_pdf(path: Path, target_mb: float, *, pages: int = 8) -> None:
    """Build a multi-page PDF roughly ``target_mb`` in size (embedded raw streams)."""
    writer = PdfWriter()
    per = max(64_000, int((target_mb * 1024 * 1024) / max(1, pages)))
    blob = (b"SCANNED-PAGE-BYTES-" * (per // 18 + 1))[:per]
    for i in range(pages):
        writer.add_blank_page(width=612, height=792)
        stream = DecodedStreamObject()
        stream.set_data(blob)
        stream_ref = writer._add_object(stream)
        writer.pages[i][NameObject("/CSPrivate")] = stream_ref
    with open(path, "wb") as handle:
        writer.write(handle)


def test_20mb_pdf_uses_page_images_never_full_source(tmp_path, monkeypatch):
    """>15 MB source → selected page images; never upload the full PDF."""
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "fat20.pdf"
    _fat_pdf(source, 20.5, pages=6)
    assert source.stat().st_size > 15 * 1024 * 1024

    uploaded: list[Path] = []

    def fake_upload(path, *, mime_type="application/pdf"):
        uploaded.append(Path(path))
        return {
            "name": "files/mock-20mb",
            "uri": "https://example.test/files/mock-20mb",
            "state": "ACTIVE",
        }

    def fake_analyze(text, file_base64, mime_type, extras=None, **kwargs):
        assert file_base64 is None
        assert kwargs.get("image_parts") or kwargs.get("file_ref")
        return {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "image/jpeg",
            "model": "gemini-test",
            "pages_sent": kwargs.get("pages_sent") or [],
            "payload_bytes": kwargs.get("payload_bytes") or 0,
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr(cl, "_pdf_page_texts", lambda _p: [f"p{i}" for i in range(6)])
    monkeypatch.setattr(
        cl,
        "_render_visual_page_parts",
        lambda _p, idxs, long_side=1024: (
            [{"inline_data": {"mime_type": "image/jpeg", "data": "qq=="}} for _ in idxs],
            [i + 1 for i in idxs],
            5000,
        ),
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "Account statement Statement Date April 1 2020 Amount Due $10",
        {"filename": "fat20.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    assert (result.extras or {}).get("document_analysis_transport") == "page_images"
    assert not uploaded, "must never upload the full large source PDF"
    assert (result.extras or {}).get("document_analysis_pages_selected") == [1, 2, 3, 4, 5, 6]


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

    for chunk in chunks:
        local_index = 0
        orig_index = chunk.page_start - 1
        assert page_bytes_fingerprint(source, orig_index) == page_bytes_fingerprint(
            chunk.path, local_index
        )


def test_planted_forgery_on_selected_last_page_reports_that_page(tmp_path, monkeypatch):
    """Long docs send first+last (+ cues); a finding on the last page keeps that page #."""
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "long.pdf"
    _blank_pdf(source, 1200)
    page_count = 1200

    def fake_analyze(text, file_base64, mime_type, extras=None, **kwargs):
        pages = kwargs.get("pages_sent") or []
        findings = []
        if page_count in pages:
            findings.append(
                {
                    "analysis_type": "text_overlay",
                    "title": "Planted forgery",
                    "observation": "Suspicious overlay near the signature block.",
                    "why_suspicious": "Ink density differs from surrounding print.",
                    "confidence": 90,
                    "severity": "medium",
                    "page": page_count,
                    "quoted_evidence": "FORGED",
                }
            )
        return {
            "findings": findings,
            "overall_note": "ok",
            "file_sent": "image/jpeg",
            "model": "gemini-test",
            "pages_sent": pages,
            "payload_bytes": kwargs.get("payload_bytes") or 0,
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 15, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_min_confidence", 70, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr(
        cl,
        "_pdf_page_texts",
        lambda _p: ["cover"] + [""] * (page_count - 2) + ["Notary Public subscribed and sworn"],
    )
    monkeypatch.setattr(
        cl,
        "_render_visual_page_parts",
        lambda _p, idxs, long_side=1024: (
            [{"inline_data": {"mime_type": "image/jpeg", "data": "qq=="}} for _ in idxs],
            [i + 1 for i in idxs],
            4000,
        ),
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "True and accurate copy annexed as Exhibit A principal balance",
        {"filename": "long.pdf", "doc_kind": "exhibit"},
        source_file=source,
        scan_profile="forensic",
    )
    selected = (result.extras or {}).get("document_analysis_pages_selected") or []
    assert 1 in selected and page_count in selected
    pages = [f.page for f in result.findings if f.page is not None]
    assert page_count in pages


def test_selected_pages_files_api_deletes_after_use(tmp_path, monkeypatch):
    """When image payload exceeds inline limit, upload slim selected_pages.pdf and delete."""
    from app.engines import contextual_logic as cl
    from app.gemini_review_state import reset_ai_review_state

    reset_ai_review_state()
    source = tmp_path / "mid.pdf"
    _blank_pdf(source, 5)

    deleted: list[str] = []
    uploaded: list[Path] = []

    def fake_upload(path, *, mime_type="application/pdf"):
        uploaded.append(Path(path))
        assert Path(path).name == "selected_pages.pdf"
        return {"name": "files/sel", "uri": "https://example.test/files/sel", "state": "ACTIVE"}

    def fake_delete(file_obj):
        if file_obj:
            deleted.append(str(file_obj.get("name") if isinstance(file_obj, dict) else file_obj))

    def fake_analyze(text, file_base64, mime_type, extras=None, **kwargs):
        assert kwargs.get("file_ref") is not None
        return {
            "findings": [],
            "overall_note": "ok",
            "file_sent": "application/pdf",
            "model": "gemini-test",
        }

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 1, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", fake_delete)
    monkeypatch.setattr(cl, "analyze_document", fake_analyze)
    monkeypatch.setattr(cl, "_pdf_page_texts", lambda _p: ["p"] * 5)
    monkeypatch.setattr(
        cl,
        "_render_visual_page_parts",
        lambda _p, idxs, long_side=1024: (
            [{"inline_data": {"mime_type": "image/jpeg", "data": "x"}} for _ in idxs],
            [i + 1 for i in idxs],
            2 * 1024 * 1024,
        ),
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    result = cl.analyze_contextual(
        "Account statement Statement Date April 1 2020 Amount Due $10",
        {"filename": "mid.pdf", "doc_kind": "statement"},
        source_file=source,
        scan_profile="forensic",
    )
    assert (result.extras or {}).get("document_analysis_transport") == "files_api_selected_pages"
    assert uploaded
    assert deleted == ["files/sel"]


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


def test_selected_pages_upload_402_reports_billing(tmp_path, monkeypatch):
    from app.engines import contextual_logic as cl
    from app.gemini_cancel import set_current_job
    from app.gemini_review_state import get_ai_review_failure, note_ai_review_failure, reset_ai_review_state

    reset_ai_review_state()
    set_current_job("b" * 32)
    source = tmp_path / "mid.pdf"
    _blank_pdf(source, 4)

    def fake_upload(path, *, mime_type="application/pdf"):
        note_ai_review_failure("billing")
        raise RuntimeError("Gemini Files upload HTTP 402: quota")

    monkeypatch.setattr(cl.settings, "gemini_api_key", "test-key", raising=False)
    monkeypatch.setattr(cl.settings, "contextual_multimodal", True, raising=False)
    monkeypatch.setattr(cl.settings, "contextual_max_upload_mb", 1, raising=False)
    monkeypatch.setattr(cl.settings, "logo_chrome_pass", False, raising=False)
    monkeypatch.setattr("app.gemini_files.upload_file_streaming", fake_upload)
    monkeypatch.setattr("app.gemini_files.wait_file_active", lambda f, **_k: f)
    monkeypatch.setattr("app.gemini_files.delete_file", lambda *_a, **_k: None)
    monkeypatch.setattr(cl, "_pdf_page_texts", lambda _p: ["p"] * 4)
    monkeypatch.setattr(
        cl,
        "_render_visual_page_parts",
        lambda _p, idxs, long_side=1024: (
            [{"inline_data": {"mime_type": "image/jpeg", "data": "x"}} for _ in idxs],
            [i + 1 for i in idxs],
            2 * 1024 * 1024,
        ),
    )
    monkeypatch.setattr("app.gemini_inflight.acquire", lambda *_a, **_k: True)
    monkeypatch.setattr("app.gemini_inflight.release", lambda *_a, **_k: None)

    cl.analyze_contextual(
        "Account statement Amount Due $1 Statement Date Jan 1 2020",
        {"filename": "mid.pdf", "doc_kind": "statement"},
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
